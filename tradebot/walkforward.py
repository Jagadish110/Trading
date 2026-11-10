"""Walk-forward validation: the test that catches curve-fitted strategies.

Tuning settings on a period and then reporting results for that same period
proves nothing. Here the settings are chosen on a training window, then
judged only on the window that follows, which they have never seen. The
windows roll forward and the unseen results are stitched together.

Requires RiskConfig.compound = False so that each day stands alone and one
simulation per parameter set can be sliced into any window.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import pandas as pd

from .backtest import Backtester, BacktestResult
from .metrics import max_drawdown, slice_result, summarize
from .strategies.base import Strategy


def param_combos(grid: dict[str, list]) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, values)) for values in product(*(grid[k] for k in keys))]


@dataclass
class WalkForwardResult:
    strategy: str
    oos_daily: pd.Series            # unseen-data P&L, stitched across folds
    oos_trades: pd.DataFrame
    oos: dict                       # metrics on unseen data: the number to trust
    best_in_sample: dict            # best parameter set over the whole period: the flattering number
    best_in_sample_params: dict
    folds: pd.DataFrame
    capital: float


def _score(daily: pd.Series, trades: pd.DataFrame, capital: float, min_trades: int) -> float:
    """Profit relative to the worst drawdown. Too few trades scores as unusable."""
    if len(trades) < min_trades:
        return float("-inf")
    dd_rs, _ = max_drawdown(daily, capital)
    return float(daily.sum()) / max(dd_rs, capital * 0.005)


def walk_forward(
    bt: Backtester,
    strategy_cls: type[Strategy],
    train_days: int = 120,
    test_days: int = 40,
    min_trades: int = 30,
) -> WalkForwardResult:
    if bt.risk_cfg.compound:
        raise ValueError("walk_forward needs RiskConfig(compound=False)")
    days = bt.days
    if len(days) < train_days + test_days:
        raise ValueError(
            f"Need at least {train_days + test_days} trading days, have {len(days)}. "
            "Fetch more history or shrink the windows."
        )
    capital = bt.risk_cfg.capital
    combos = param_combos(strategy_cls.param_grid)
    runs: list[BacktestResult] = [bt.run(strategy_cls(**p)) for p in combos]

    oos_daily, oos_trades, folds = [], [], []
    start = 0
    while start + train_days < len(days):
        train = days[start:start + train_days]
        test = days[start + train_days:start + train_days + test_days]
        scores = [_score(*slice_result(r.daily, r.trades, train), capital, min_trades) for r in runs]
        best = max(range(len(runs)), key=scores.__getitem__)
        # Only trade the next window if the best settings actually made money in training.
        traded = scores[best] > 0
        if traded:
            d, t = slice_result(runs[best].daily, runs[best].trades, test)
        else:
            d, t = pd.Series(0.0, index=test), runs[best].trades.iloc[0:0]
        oos_daily.append(d)
        oos_trades.append(t)
        folds.append({
            "train_from": train[0].date(), "train_to": train[-1].date(),
            "test_from": test[0].date(), "test_to": test[-1].date(),
            "traded": traded,
            "params": combos[best] if traded else None,
            "train_score": round(scores[best], 2) if scores[best] != float("-inf") else None,
            "test_net": round(float(d.sum()), 0),
            "test_trades": len(t),
        })
        start += test_days

    daily = pd.concat(oos_daily)
    trades = pd.concat(oos_trades, ignore_index=True)
    full = [float(r.daily.sum()) for r in runs]
    top = max(range(len(runs)), key=full.__getitem__)
    return WalkForwardResult(
        strategy=strategy_cls.name,
        oos_daily=daily,
        oos_trades=trades,
        oos=summarize(daily, trades, capital),
        best_in_sample=summarize(runs[top].daily, runs[top].trades, capital),
        best_in_sample_params=combos[top],
        folds=pd.DataFrame(folds),
        capital=capital,
    )


def verdict(wf: WalkForwardResult, min_trades: int = 100) -> tuple[str, str]:
    """A coarse screen on unseen-data results. Passing is a reason to paper
    trade, not a promise of profit."""
    m = wf.oos
    if not wf.folds["traded"].any():
        return "FAIL", "lost money in every training window, so it was never traded"
    if m["wiped"]:
        return "FAIL", "wiped out the account on unseen data"
    if m["trades"] < min_trades:
        return "TOO FEW TRADES", f"only {m['trades']} unseen trades; need {min_trades}+ to judge"
    if m["net_pnl"] <= 0:
        return "FAIL", "lost money on unseen data after costs"
    if m["profit_factor"] < 1.15:
        return "FAIL", f"profit factor {m['profit_factor']:.2f} is too thin to survive real fills"
    if m["max_dd_pct"] > 15:
        return "FAIL", f"drawdown {m['max_dd_pct']:.1f}% is too deep"
    return "PASS", "profitable on unseen data; next step is paper trading"
