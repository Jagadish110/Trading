"""Swing trading: the book's rules, the daily scan and the price download."""
import json
from datetime import date, datetime

import pandas as pd
import pytest

from tradebot.strategies.base import Strategy
from tradebot.swing.book import SwingBacktester, SwingBook
from tradebot.swing.config import DeliveryCosts, delivery_charges, swing_risk
from tradebot.swing.data import drop_unfinished, fetch_yahoo, load_daily, make_daily_synthetic, refresh, save_daily
from tradebot.swing.service import SwingService
from tradebot.swing.strategies import SWING_REGISTRY

NO_SLIP = DeliveryCosts(slippage_bps=0.0)
RISK = swing_risk(20_000)                 # 1% risk = Rs 200, position cap 30% = Rs 6,000
BUY = (1, 95.0, 110.0, False)             # buy signal: stop 95, target 110
NONE = (0, float("nan"), float("nan"), False)


def book():
    return SwingBook(RISK, NO_SLIP, max_hold_days=3)


def kinds(events):
    return [(e["kind"], e["symbol"]) for e in events]


def test_delivery_charges_hand_calculation():
    # buy 10,000, sell 10,500: STT 20.5, txn 0.62935, SEBI 0.0205, GST 0.116973, stamp 1.5, DP 15.34
    assert delivery_charges(10_000, 10_500, DeliveryCosts()) == pytest.approx(38.1068, abs=1e-3)


def test_buy_is_announced_then_filled_at_next_open():
    b = book()
    ev = b.step("2026-01-05", {"AAA": (100, 101, 99, 100)}, {"AAA": BUY})
    assert kinds(ev) == [("buy_tomorrow", "AAA")] and ev[0]["qty"] == 40      # Rs 200 / Rs 5 risk
    assert b.positions == {}
    ev = b.step("2026-01-06", {"AAA": (101, 102, 100, 101)}, {"AAA": NONE})
    assert kinds(ev) == [("bought", "AAA")]
    p = b.positions["AAA"]
    assert (p["entry_price"], p["qty"], p["entry_day"]) == (101, 33, "2026-01-06")   # Rs 200 / Rs 6 risk


def test_buy_is_cancelled_if_it_opens_beyond_stop_or_target():
    for open_ in (94.0, 111.0):
        b = book()
        b.step("2026-01-05", {"AAA": (100, 101, 99, 100)}, {"AAA": BUY})
        ev = b.step("2026-01-06", {"AAA": (open_, open_ + 1, open_ - 1, open_)}, {"AAA": NONE})
        assert kinds(ev) == [("skipped", "AAA")] and b.positions == {}


def held():
    b = book()
    b.step("2026-01-05", {"AAA": (100, 101, 99, 100)}, {"AAA": BUY})
    b.step("2026-01-06", {"AAA": (100, 101, 99, 100)}, {"AAA": NONE})
    return b


def test_stop_wins_over_target_and_gap_fills_at_open():
    b = held()
    ev = b.step("2026-01-07", {"AAA": (100, 111, 94, 100)}, {"AAA": NONE})
    assert (ev[0]["reason"], ev[0]["exit_price"]) == ("stop", 95.0) and ev[0]["net"] < 0
    b = held()
    ev = b.step("2026-01-07", {"AAA": (92, 93, 91, 92)}, {"AAA": NONE})
    assert (ev[0]["reason"], ev[0]["exit_price"]) == ("stop", 92.0)


def test_target_exit_and_profit_after_costs():
    b = held()
    ev = b.step("2026-01-07", {"AAA": (100, 111, 99, 105)}, {"AAA": NONE})
    t = ev[0]
    assert (t["reason"], t["exit_price"], t["qty"]) == ("target", 110.0, 40)
    assert t["gross"] == pytest.approx(400.0)
    assert t["net"] == pytest.approx(400.0 - delivery_charges(4000, 4400, NO_SLIP))


def test_exit_signal_and_time_limit_sell_at_the_next_open():
    b = held()
    ev = b.step("2026-01-07", {"AAA": (100, 101, 99, 100)}, {"AAA": (0, 0, 0, True)})
    assert kinds(ev) == [("sell_tomorrow", "AAA")] and ev[0]["reason"] == "signal_exit"
    ev = b.step("2026-01-08", {"AAA": (102, 103, 101, 102)}, {"AAA": NONE})
    assert (ev[0]["kind"], ev[0]["reason"], ev[0]["exit_price"]) == ("sold", "signal_exit", 102)

    b = held()                                            # max_hold_days = 3
    for day in ("2026-01-07", "2026-01-08"):
        ev = b.step(day, {"AAA": (100, 101, 99, 100)}, {"AAA": NONE})
    assert kinds(ev) == [("sell_tomorrow", "AAA")] and ev[0]["reason"] == "time_exit"


def test_only_three_positions_and_never_short():
    b = book()
    bars = {s: (100, 101, 99, 100) for s in "ABCDE"}
    ev = b.step("2026-01-05", bars, {**{s: BUY for s in "ABCD"}, "E": (-1, 105.0, 90.0, False)})
    assert kinds(ev) == [("buy_tomorrow", s) for s in "ABC"]
    b.step("2026-01-06", bars, {s: BUY for s in "ABCDE"})
    assert sorted(b.positions) == ["A", "B", "C"] and b.pending_buys == {}


def test_expensive_stock_is_skipped_not_oversized():
    b = book()
    ev = b.step("2026-01-05", {"MRF": (9000, 9100, 8900, 9000)}, {"MRF": (1, 8500.0, 10000.0, False)})
    assert ev == []                                       # one share costs more than the Rs 6,000 cap


def test_state_survives_a_restart():
    b = held()
    again = SwingBook(RISK, NO_SLIP, state=json.loads(json.dumps(b.state())))
    ev = again.step("2026-01-07", {"AAA": (100, 111, 99, 105)}, {"AAA": NONE})
    assert ev[0]["reason"] == "target" and again.last_day == "2026-01-07"


# -- strategies ------------------------------------------------------------
@pytest.fixture(scope="module")
def market():
    return make_daily_synthetic([f"S{i:02d}" for i in range(12)], days=520, seed=4)


@pytest.mark.parametrize("name", list(SWING_REGISTRY))
def test_swing_strategies_trade_long_only_and_cannot_see_ahead(market, name):
    strategy = SWING_REGISTRY[name]()
    df = market["S00"]
    full = strategy.signals(df)
    assert (full["side"] >= 0).all()
    cut = strategy.signals(df.iloc[:400])
    pd.testing.assert_frame_equal(full.iloc[:400], cut)
    res = SwingBacktester(market).run(strategy)
    assert len(res.trades) > 10 and (res.trades["side"] == 1).all()
    assert res.daily.sum() == pytest.approx(res.trades["net"].sum())
    assert (res.trades["entry_time"] <= res.trades["exit_time"]).all()


# -- the daily scan ----------------------------------------------------------
class Cheat(Strategy):
    """Looks five days ahead. Only to give the scan something that passes."""
    name = "cheat"
    default_params = {"rr": 2.0}
    param_grid = {"rr": [2.0]}

    def signals(self, df):
        out = self.empty(df)
        ahead = df["close"].shift(-5) / df["close"] - 1
        go = (ahead > 0.04) | ahead.isna()               # the newest days always signal, for the scan test
        return self.fill(out, go, go & False, df["close"], long_stop=df["close"] * 0.96,
                         short_stop=df["close"], rr=self.params["rr"])


def service(tmp_path, market, registry, today="2026-11-02"):
    svc = SwingService(tmp_path, 20_000, registry=registry, today=lambda: date.fromisoformat(today),
                       refresh_data=lambda symbols, folder, say=None: [])
    save_daily(market, svc.prices)
    return svc


def test_scan_refuses_to_buy_when_nothing_passes(tmp_path, market):
    text = service(tmp_path, market, SWING_REGISTRY).run()
    assert "NO new buys" in text and "Nothing to do tomorrow" in text


def test_scan_gives_full_buy_instructions_then_tracks_the_position(tmp_path, market):
    cut = {s: df.iloc[:-6] for s, df in market.items()}     # the cheat needs 5 days of future
    svc = service(tmp_path, cut, {"cheat": Cheat})
    first = svc.run()
    assert "Strategy in use: cheat" in first and "BUY next trading morning" in first
    line = next(l for l in first.splitlines() if l.startswith("• Buy "))
    assert "set a GTT with stop-loss" in line and "Risk about Rs" in line and "Do not buy if it opens" in line
    symbol = line.split()[3]

    save_daily({s: df.iloc[:-5] for s, df in market.items()}, svc.prices)      # one more day arrives
    second = svc.run()
    assert f"{symbol}: assumed bought" in second or f"{symbol}: buy cancelled" in second
    state = json.loads(svc.state_path.read_text())
    assert state["last_day"] == str(market["S00"].index[-6].date()) and state["strategy"] == "cheat"

    held = list(state["positions"])
    if held:
        assert held[0] in svc.holdings()
        assert "removed" in svc.skip(held[0].lower())
        assert held[0] not in json.loads(svc.state_path.read_text())["positions"]
    assert "not in the swing list" in svc.skip("NOPE")


def test_scan_with_no_price_files_says_so(tmp_path):
    svc = SwingService(tmp_path, refresh_data=lambda symbols, folder, say=None: list(symbols))
    assert "nothing to scan" in svc.run()


# -- price download ----------------------------------------------------------
YAHOO = {"chart": {"result": [{
    "timestamp": [1762141500, 1762227900, 1762314300],          # 09:15 IST on 3, 4, 5 Nov 2025
    "indicators": {
        "quote": [{"open": [100.0, 102.0, None], "high": [103.0, 104.0, None], "low": [99.0, 101.0, None],
                   "close": [102.0, 103.0, None], "volume": [1000, 1200, None]}],
        "adjclose": [{"adjclose": [51.0, 103.0, None]}],          # a 2-for-1 split before day two
    }}]}}


def test_yahoo_candles_are_parsed_and_split_adjusted():
    seen = []
    df = fetch_yahoo("M&M", get=lambda url: seen.append(url) or json.dumps(YAHOO).encode())
    assert "M%26M.NS" in seen[0]
    assert list(df.index.strftime("%Y-%m-%d")) == ["2025-11-03", "2025-11-04"]     # empty row dropped
    assert df.iloc[0].tolist() == [50.0, 51.5, 49.5, 51.0, 1000.0]
    assert df.iloc[1]["close"] == 103.0


def test_todays_unfinished_candle_is_dropped():
    df = make_daily_synthetic(["A"], days=5, end="2026-10-30")["A"]
    assert len(drop_unfinished(df, datetime(2026, 10, 30, 11, 0))) == 4
    assert len(drop_unfinished(df, datetime(2026, 10, 30, 16, 0))) == 5
    assert len(drop_unfinished(df, datetime(2026, 10, 31, 11, 0))) == 5


def test_refresh_saves_files_and_reports_failures(tmp_path):
    good = make_daily_synthetic(["A"], days=80)["A"]
    payload = {"chart": {"result": [{
        "timestamp": [int(t.timestamp()) + 13500 for t in good.index.tz_localize("UTC")],
        "indicators": {"quote": [{c: good[c].tolist() for c in good.columns}]}}]}}

    def get(url):
        if "BAD" in url:
            raise OSError("refused")
        return json.dumps(payload).encode()
    said = []
    failed = refresh(["GOOD", "BAD"], tmp_path, get=get, say=said.append, sleep=lambda s: None,
                     now=lambda: datetime(2026, 11, 20, 18, 0))
    assert failed == ["BAD"] and "BAD" in said[0]
    loaded = load_daily(tmp_path)
    assert list(loaded) == ["GOOD"] and len(loaded["GOOD"]) == 80
    assert loaded["GOOD"]["close"].iloc[-1] == pytest.approx(good["close"].iloc[-1], abs=1e-3)
