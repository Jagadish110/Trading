"""The backtester's honesty rules, each pinned by a hand-built scenario."""
import pandas as pd
import pytest

from conftest import Scripted, make_day
from tradebot.backtest import Backtester
from tradebot.config import CostConfig, RiskConfig
from tradebot.costs import round_trip_charges

NO_SLIP = CostConfig(slippage_bps=0.0)
RISK = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_position_pct=100.0)


def run(df, plan, exits=None, risk=RISK, costs=NO_SLIP, **kw):
    bt = Backtester({"TEST": df}, risk=risk, costs=costs, **kw)
    return bt.run(Scripted(plan, exits))


def set_bar(df, ts, **cols):
    for k, v in cols.items():
        df.loc[pd.Timestamp(ts), k] = v


def test_charges_match_hand_calculation():
    # buy 1,00,000 and sell 1,00,500 at Zerodha intraday rates
    # brokerage 20+20, STT 25.125, txn 6.15535, SEBI 0.2005, GST 8.344053, stamp 3.0
    assert round_trip_charges(100_000, 100_500, CostConfig()) == pytest.approx(82.8249, abs=1e-3)


def test_entry_is_next_candle_open_not_signal_close(flat_day):
    set_bar(flat_day, "2026-01-05 10:00", close=101.0, high=101.5)
    set_bar(flat_day, "2026-01-05 10:05", open=102.0, high=102.5, low=101.5, close=102.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 95.0, 120.0)})
    t = res.trades.iloc[0]
    assert t.entry_time == pd.Timestamp("2026-01-05 10:05")
    assert t.entry_price == 102.0


def test_position_size_risks_the_configured_amount(flat_day):
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 110.0)})
    # 1% of 1,00,000 = 1,000 at risk, 2 rupees per share -> 500 shares
    assert res.trades.iloc[0].qty == 500


def test_position_value_cap_limits_size(flat_day):
    risk = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_position_pct=20.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 110.0)}, risk=risk)
    assert res.trades.iloc[0].qty == 200          # 20,000 / 100


def test_stop_wins_when_candle_hits_both(flat_day):
    set_bar(flat_day, "2026-01-05 10:10", high=106.0, low=97.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 105.0)})
    t = res.trades.iloc[0]
    assert (t.reason, t.exit_price) == ("stop", 98.0)
    assert t.net < 0


def test_target_exit(flat_day):
    set_bar(flat_day, "2026-01-05 10:10", high=106.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 105.0)})
    t = res.trades.iloc[0]
    assert (t.reason, t.exit_price) == ("target", 105.0)
    assert t.gross == pytest.approx(5.0 * 500)
    assert t.net == pytest.approx(t.gross - t.charges)


def test_gap_through_stop_fills_at_open(flat_day):
    set_bar(flat_day, "2026-01-05 10:10", open=96.0, high=96.5, low=95.5, close=96.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 105.0)})
    t = res.trades.iloc[0]
    assert (t.reason, t.exit_price) == ("stop", 96.0)


def test_short_trade_mirrors_long(flat_day):
    set_bar(flat_day, "2026-01-05 10:10", low=94.0)
    res = run(flat_day, {"2026-01-05 10:00": (-1, 102.0, 95.0)})
    t = res.trades.iloc[0]
    assert (t.side, t.reason, t.exit_price) == (-1, "target", 95.0)
    assert t.gross == pytest.approx(5.0 * 500)


def test_shorts_can_be_disabled(flat_day):
    res = run(flat_day, {"2026-01-05 10:00": (-1, 102.0, 95.0)}, allow_short=False)
    assert len(res.trades) == 0 and res.skipped["shorts_off"] == 1


def test_square_off_at_1515(flat_day):
    res = run(flat_day, {"2026-01-05 10:00": (1, 90.0, 120.0)})
    t = res.trades.iloc[0]
    assert t.reason == "square_off"
    assert t.exit_time == pd.Timestamp("2026-01-05 15:15")


def test_no_entry_after_last_entry_time(flat_day):
    res = run(flat_day, {"2026-01-05 14:45": (1, 98.0, 105.0)})   # would enter at 14:50
    assert len(res.trades) == 0


def test_signal_on_last_candle_does_not_carry_to_next_day():
    df = pd.concat([make_day("2026-01-05", [100.0] * 75), make_day("2026-01-06", [100.0] * 75)])
    res = run(df, {"2026-01-05 15:25": (1, 98.0, 105.0)})
    assert len(res.trades) == 0


def test_entry_skipped_when_open_gaps_past_stop(flat_day):
    set_bar(flat_day, "2026-01-05 10:05", open=97.0, high=97.5, low=96.5, close=97.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 105.0)})
    assert len(res.trades) == 0 and res.skipped["bad_levels"] == 1


def test_signal_exit_closes_at_next_open(flat_day):
    res = run(flat_day, {"2026-01-05 10:00": (1, 90.0, 120.0)},
              exits={"2026-01-05 11:00": "exit_long"})
    t = res.trades.iloc[0]
    assert (t.reason, t.exit_time) == ("signal_exit", pd.Timestamp("2026-01-05 11:05"))


def test_daily_loss_limit_blocks_new_entries(flat_day):
    # 1% risk per trade, 1.5% daily limit: two stops (about -2%) must block the third entry
    risk = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_daily_loss_pct=1.5,
                      max_position_pct=100.0)
    for ts in ("2026-01-05 10:10", "2026-01-05 11:10"):
        set_bar(flat_day, ts, low=97.0)
    plan = {f"2026-01-05 {t}": (1, 98.0, 110.0) for t in ("10:00", "11:00", "12:00")}
    res = run(flat_day, plan, risk=risk)
    assert len(res.trades) == 2
    assert res.skipped["risk_gate"] == 1
    assert res.daily.iloc[0] < -1500


def test_max_trades_per_day(flat_day):
    risk = RiskConfig(capital=100_000, max_trades_per_day=1, max_position_pct=100.0)
    set_bar(flat_day, "2026-01-05 10:10", high=106.0)
    plan = {"2026-01-05 10:00": (1, 98.0, 105.0), "2026-01-05 11:00": (1, 98.0, 105.0)}
    assert len(run(flat_day, plan, risk=risk).trades) == 1


def test_max_open_positions_across_symbols(flat_day):
    risk = RiskConfig(capital=100_000, max_open_positions=1, max_position_pct=100.0)
    bt = Backtester({"AAA": flat_day, "BBB": flat_day.copy()}, risk=risk, costs=NO_SLIP)
    res = bt.run(Scripted({"2026-01-05 10:00": (1, 90.0, 120.0)}))
    assert list(res.trades.symbol) == ["AAA"]


def test_slippage_makes_fills_worse(flat_day):
    set_bar(flat_day, "2026-01-05 10:10", low=97.0)
    res = run(flat_day, {"2026-01-05 10:00": (1, 98.0, 105.0)}, costs=CostConfig(slippage_bps=10))
    t = res.trades.iloc[0]
    assert t.entry_price == pytest.approx(100.1)
    assert t.exit_price == pytest.approx(98.0 * 0.999)


def test_daily_series_covers_every_day_and_sums_to_trades():
    df = pd.concat([make_day("2026-01-05", [100.0] * 75), make_day("2026-01-06", [100.0] * 75)])
    res = run(df, {"2026-01-06 10:00": (1, 90.0, 120.0)})
    assert len(res.daily) == 2 and res.daily.iloc[0] == 0
    assert res.daily.sum() == pytest.approx(res.trades.net.sum())
