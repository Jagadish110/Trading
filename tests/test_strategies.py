"""Strategies must not peek at the future, and must emit sane levels."""
import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import Backtester
from tradebot.data import make_synthetic
from tradebot.indicators import rsi, session_vwap
from tradebot.metrics import max_drawdown, summarize
from tradebot.strategies import REGISTRY
from tradebot.walkforward import param_combos, walk_forward


@pytest.fixture(scope="module")
def candles():
    return make_synthetic(["AAA", "BBB"], days=40, seed=3)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_no_lookahead(name, candles):
    """Signals on the first N candles must not change when later candles are added."""
    df = candles["AAA"]
    full = REGISTRY[name]().signals(df)
    for cut in (410, 1337, 2222):
        part = REGISTRY[name]().signals(df.iloc[:cut])
        ref = full.iloc[:cut]
        assert (part["side"] == ref["side"]).all()
        assert (part["exit_long"] == ref["exit_long"]).all()
        assert (part["exit_short"] == ref["exit_short"]).all()
        live = ref["side"] != 0
        np.testing.assert_allclose(part.loc[live, "stop"], ref.loc[live, "stop"])
        np.testing.assert_allclose(part.loc[live, "target"], ref.loc[live, "target"])


@pytest.mark.parametrize("name", list(REGISTRY))
def test_levels_sit_on_the_correct_side(name, candles):
    df = candles["AAA"]
    sig = REGISTRY[name]().signals(df)
    assert (sig["side"] != 0).sum() > 0, "strategy never fired on 40 days of data"
    longs, shorts = sig[sig["side"] == 1], sig[sig["side"] == -1]
    assert (longs["stop"] < df.loc[longs.index, "close"]).all()
    assert (longs["target"] > df.loc[longs.index, "close"]).all()
    assert (shorts["stop"] > df.loc[shorts.index, "close"]).all()
    assert (shorts["target"] < df.loc[shorts.index, "close"]).all()


@pytest.mark.parametrize("name", list(REGISTRY))
def test_every_grid_combination_runs(name, candles):
    bt = Backtester(candles)
    for params in param_combos(REGISTRY[name].param_grid):
        res = bt.run(REGISTRY[name](**params))
        assert res.daily.sum() == pytest.approx(res.trades["net"].sum() if len(res.trades) else 0.0)
        if len(res.trades):
            same_day = res.trades["entry_time"].dt.date == res.trades["exit_time"].dt.date
            assert same_day.all(), "a position was held overnight"
            assert (res.trades["exit_time"].dt.time <= pd.Timestamp("15:15").time()).all()


def test_unknown_param_is_rejected():
    with pytest.raises(ValueError):
        REGISTRY["orb"](not_a_param=1)


def test_vwap_resets_each_day(candles):
    df = candles["AAA"]
    vwap = session_vwap(df)
    first = df.groupby(df.index.normalize()).head(1)
    typical = (first["high"] + first["low"] + first["close"]) / 3
    np.testing.assert_allclose(vwap.loc[first.index], typical)


def test_rsi_stays_in_range(candles):
    r = rsi(candles["AAA"]["close"], 14).dropna()
    assert r.between(0, 100).all()


def test_max_drawdown():
    daily = pd.Series([1000.0, -3000.0, 500.0, 4000.0])
    rs, pct = max_drawdown(daily, 100_000)
    assert rs == 3000.0
    assert pct == pytest.approx(3000 / 101_000 * 100)


def test_summary_of_no_trades_is_all_zero(candles):
    bt = Backtester(candles)
    empty = bt.run(REGISTRY["orb"](min_range_pct=50.0))      # impossible filter
    m = summarize(empty.daily, empty.trades, 100_000)
    assert m["trades"] == 0 and m["net_pnl"] == 0 and m["max_dd_pct"] == 0


def test_walk_forward_only_reports_unseen_days(candles):
    bt = Backtester(candles)
    wf = walk_forward(bt, REGISTRY["orb"], train_days=20, test_days=10, min_trades=5)
    assert len(wf.oos_daily) == 20                           # 40 days minus the first training window
    assert wf.oos_daily.index[0] == bt.days[20]
    assert wf.oos_daily.index.is_unique
    if len(wf.oos_trades):
        assert (wf.oos_trades["exit_time"].dt.normalize() >= bt.days[20]).all()
    for _, f in wf.folds.iterrows():
        assert f.train_to < f.test_from
