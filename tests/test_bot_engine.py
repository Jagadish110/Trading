"""Paper trading engine: it must agree with the backtester, and the extra
safety controls (loss limit, pause, kill switch, restart) must work."""
import pandas as pd
import pytest

from conftest import Scripted, make_day
from tradebot.backtest import Backtester
from tradebot.bot.broker import PaperBroker
from tradebot.bot.engine import EngineConfig, TradingEngine
from tradebot.bot.runner import replay
from tradebot.bot.store import Store
from tradebot.config import CostConfig, RiskConfig
from tradebot.data import make_synthetic
from tradebot.strategies import REGISTRY

SAME_AS_BACKTEST = EngineConfig(flatten_on_daily_loss=False)
COMPARE = ["symbol", "side", "qty", "entry_time", "entry_price", "exit_time", "exit_price",
           "stop", "target", "reason", "gross", "charges", "net"]
RISK = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_position_pct=100.0)
NO_SLIP = CostConfig(slippage_bps=0.0)


@pytest.fixture(scope="module")
def market():
    return make_synthetic(["AAA", "BBB", "CCC", "DDD"], days=12, seed=3)


def engine_trades(strategy, data, precompute, cfg=SAME_AS_BACKTEST, **kw):
    engine = TradingEngine(strategy, PaperBroker(kw.get("costs")), cfg=cfg, **kw)
    replay(engine, data, precompute=precompute)
    df = pd.DataFrame(engine.trades, columns=COMPARE)
    return engine, df.sort_values(["entry_time", "symbol"]).reset_index(drop=True)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_replay_matches_backtester_trade_for_trade(market, name):
    strategy = REGISTRY[name]()
    expected = Backtester(market).run(strategy)
    assert len(expected.trades) > 5, "scenario too quiet to prove anything"
    engine, got = engine_trades(strategy, market, precompute=True)
    pd.testing.assert_frame_equal(got, expected.trades[COMPARE], check_dtype=False)
    assert {k: engine.skipped[k] for k in expected.skipped} == expected.skipped


@pytest.mark.parametrize("name", ["orb", "ema_trend", "supertrend"])
def test_candle_by_candle_signals_match_backtester(market, name):
    """The live path: signals recomputed as each candle arrives."""
    small = {s: df[df.index >= df.index[-1].normalize() - pd.Timedelta(days=4)]
             for s, df in list(market.items())[:3]}
    strategy = REGISTRY[name]()
    expected = Backtester(small).run(strategy)
    assert len(expected.trades) > 0
    _, got = engine_trades(strategy, small, precompute=False)
    pd.testing.assert_frame_equal(got, expected.trades[COMPARE], check_dtype=False)


def test_results_are_saved_to_the_database(market):
    store = Store()
    engine = TradingEngine(REGISTRY["orb"](), PaperBroker(), store, cfg=SAME_AS_BACKTEST)
    replay(engine, market)
    totals = store.totals("paper")
    assert totals["trades"] == len(engine.trades) > 0
    assert totals["net"] == pytest.approx(sum(t["net"] for t in engine.trades))
    assert store.open_positions("paper") == []
    assert sum(d["trades"] for d in store.daily("paper")) == totals["trades"]


# -- hand-built scenarios ------------------------------------------------
def two_symbols():
    a, b = make_day("2026-01-05", [100.0] * 75), make_day("2026-01-05", [100.0] * 75)
    return {"AAA": a, "BBB": b}


def run(data, plan, cfg=None, risk=RISK, store=None):
    engine = TradingEngine(Scripted(plan), PaperBroker(NO_SLIP), store, risk, costs=NO_SLIP, cfg=cfg)
    replay(engine, data, precompute=False)
    return engine


def test_daily_loss_limit_closes_open_positions():
    data = two_symbols()
    # Both go long 500 shares at 100. The stops are far away, so only the
    # loss limit (2% = Rs 2,000) can get the bot out.
    for df in data.values():
        df.loc[pd.Timestamp("2026-01-05 10:30"):, ["open", "high", "low", "close"]] = [97.5, 98.0, 97.0, 97.5]
        df.loc[pd.Timestamp("2026-01-05 10:30"), "open"] = 100.0
    wide = {"2026-01-05 10:00": (1, 90.0, 110.0)}
    risk_wide = RiskConfig(capital=100_000, risk_per_trade_pct=5.0, max_position_pct=50.0)

    engine = run(data, wide, risk=risk_wide)             # 500 shares each, stop at 90
    assert [t["reason"] for t in engine.trades] == ["daily_loss", "daily_loss"]
    assert engine.status == "stopped" and engine.risk.halted
    assert engine.trades[0]["exit_time"] == pd.Timestamp("2026-01-05 10:30")
    assert sum(t["net"] for t in engine.trades) < -2_000

    off = run(data, wide, risk=risk_wide, cfg=SAME_AS_BACKTEST)
    assert [t["reason"] for t in off.trades] == ["square_off", "square_off"]


def test_no_entries_after_the_loss_limit_trips():
    data = two_symbols()
    for df in data.values():
        df.loc[pd.Timestamp("2026-01-05 10:30"):, ["open", "high", "low", "close"]] = [97.5, 98.0, 97.0, 97.5]
    plan = {"2026-01-05 10:00": (1, 90.0, 110.0), "2026-01-05 12:00": (1, 90.0, 110.0)}
    engine = run(data, plan, risk=RiskConfig(capital=100_000, risk_per_trade_pct=5.0))
    assert len(engine.trades) == 2
    assert engine.skipped["risk_gate"] == 2


def test_pause_blocks_entries_and_kill_switch_closes_everything():
    engine = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), risk=RISK, costs=NO_SLIP)
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("AAA", t, 1, 98.0, 110.0, False, False, close=100.0)
    engine.on_open("AAA", t + pd.Timedelta(minutes=5), 100.0)
    assert list(engine.positions) == ["AAA"]

    engine.pause()
    engine.set_intent("BBB", t, 1, 98.0, 110.0, False, False, close=100.0)
    engine.on_open("BBB", t + pd.Timedelta(minutes=5), 100.0)
    assert "BBB" not in engine.positions and engine.skipped["risk_gate"] == 1

    engine.on_tick("AAA", t + pd.Timedelta(minutes=6), 101.0)
    engine.kill()
    assert engine.positions == {}
    assert engine.trades[0]["reason"] == "manual" and engine.trades[0]["exit_price"] == 101.0
    engine.resume()
    assert engine.status == "running" and not engine.risk.halted


def test_live_ticks_hit_stop_target_and_square_off():
    engine = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), risk=RISK, costs=NO_SLIP)
    t = pd.Timestamp("2026-01-05 10:00")
    for sym, tick, reason, price in [("AAA", 97.9, "stop", 97.9), ("BBB", 105.2, "target", 105.0)]:
        engine.set_intent(sym, t, 1, 98.0, 105.0, False, False)
        engine.on_open(sym, t + pd.Timedelta(minutes=5), 100.0)
        engine.on_tick(sym, t + pd.Timedelta(minutes=6), 100.5)
        assert sym in engine.positions
        engine.on_tick(sym, t + pd.Timedelta(minutes=7), tick)
        assert (engine.trades[-1]["reason"], engine.trades[-1]["exit_price"]) == (reason, price)
    engine.set_intent("CCC", t, -1, 102.0, 95.0, False, False)
    engine.on_open("CCC", t + pd.Timedelta(minutes=5), 100.0)
    engine.on_tick("CCC", pd.Timestamp("2026-01-05 15:15:01"), 99.0)
    assert (engine.trades[-1]["reason"], engine.trades[-1]["side"]) == ("square_off", -1)
    assert engine.trades[-1]["gross"] == pytest.approx(1.0 * engine.trades[-1]["qty"])


def test_stale_signal_from_yesterday_is_ignored():
    engine = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), risk=RISK, costs=NO_SLIP)
    engine.set_intent("AAA", pd.Timestamp("2026-01-05 15:25"), 1, 98.0, 110.0, False, False)
    engine.on_open("AAA", pd.Timestamp("2026-01-06 09:20"), 100.0)
    assert engine.positions == {}


def test_restart_resumes_open_position_and_todays_counts(tmp_path):
    db = tmp_path / "bot.db"
    t = pd.Timestamp("2026-01-05 10:00")
    first = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), Store(db), RISK, costs=NO_SLIP)
    for sym in ("AAA", "BBB"):
        first.set_intent(sym, t, 1, 98.0, 105.0, False, False)
        first.on_open(sym, t + pd.Timedelta(minutes=5), 100.0)
    first.on_tick("AAA", t + pd.Timedelta(minutes=6), 97.0)        # AAA stopped out
    first.store.close()

    second = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), Store(db), RISK, costs=NO_SLIP)
    second.restore(today=t.date())
    assert list(second.positions) == ["BBB"]
    assert second.risk.trades_today == 2
    assert second.risk.day_pnl == pytest.approx(first.risk.day_pnl) and second.risk.day_pnl < 0
    second.on_tick("BBB", t + pd.Timedelta(minutes=9), 105.0)
    assert second.trades[0]["reason"] == "target"
    assert second.store.open_positions("paper") == []

    third = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), Store(db), RISK, costs=NO_SLIP)
    third.restore(today=(t + pd.Timedelta(days=1)).date())         # next day starts clean
    assert third.risk.trades_today == 0 and third.risk.day_pnl == 0
    assert third.risk.total_pnl == pytest.approx(second.risk.total_pnl)


def test_position_left_from_an_earlier_day_is_dropped_with_a_warning(tmp_path):
    db = tmp_path / "bot.db"
    t = pd.Timestamp("2026-01-05 10:00")
    first = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), Store(db), RISK, costs=NO_SLIP)
    first.set_intent("AAA", t, 1, 98.0, 105.0, False, False)
    first.on_open("AAA", t + pd.Timedelta(minutes=5), 100.0)
    first.store.close()
    second = TradingEngine(Scripted({}), PaperBroker(NO_SLIP), Store(db), RISK, costs=NO_SLIP)
    second.restore(today=pd.Timestamp("2026-01-06").date())
    assert second.positions == {}
    assert any("dropped" in e["message"] for e in second.store.events())
