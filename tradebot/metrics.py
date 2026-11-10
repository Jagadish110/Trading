"""Turn a list of trades and a daily P&L series into the numbers that matter."""
from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(daily: pd.Series, capital: float) -> tuple[float, float]:
    """Worst peak-to-trough fall of the equity curve: (rupees, % of the peak)."""
    if not len(daily):
        return 0.0, 0.0
    equity = capital + daily.cumsum()
    peak = np.maximum(equity.cummax(), capital)
    dd = equity - peak
    worst = dd.idxmin()
    worst_rs = max(0.0, float(-dd.min()))
    return worst_rs, min(100.0, worst_rs / float(peak[worst]) * 100)


def summarize(daily: pd.Series, trades: pd.DataFrame, capital: float) -> dict:
    n = len(trades)
    net = float(daily.sum())
    dd_rs, dd_pct = max_drawdown(daily, capital)
    std = float(daily.std(ddof=0)) if len(daily) > 1 else 0.0
    # With fixed sizing the simulation keeps trading after the money is gone,
    # so losses can exceed the capital. A real account would simply be dead.
    wiped = bool(len(daily)) and bool((capital + daily.cumsum() <= 0).any())
    out = {
        "wiped": wiped,
        "days": int(len(daily)),
        "trades": int(n),
        "net_pnl": net,
        "return_pct": max(-100.0, net / capital * 100),
        "max_dd_pct": dd_pct,
        "max_dd_rs": dd_rs,
        "sharpe": float(daily.mean() / std * np.sqrt(252)) if std > 0 else 0.0,
        "win_rate": 0.0, "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "expectancy": 0.0, "gross_pnl": 0.0, "charges": 0.0,
        "trades_per_day": n / len(daily) if len(daily) else 0.0,
        "green_days_pct": 0.0,
    }
    if n:
        wins, losses = trades.loc[trades["net"] > 0, "net"], trades.loc[trades["net"] <= 0, "net"]
        active = daily[daily != 0]
        out.update(
            win_rate=len(wins) / n * 100,
            profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
            avg_win=float(wins.mean()) if len(wins) else 0.0,
            avg_loss=float(losses.mean()) if len(losses) else 0.0,
            expectancy=float(trades["net"].mean()),
            gross_pnl=float(trades["gross"].sum()),
            charges=float(trades["charges"].sum()),
            green_days_pct=float((active > 0).mean() * 100) if len(active) else 0.0,
        )
    return out


def slice_result(daily: pd.Series, trades: pd.DataFrame, days: pd.DatetimeIndex):
    """The part of a backtest that falls on the given trading days."""
    d = daily.reindex(days, fill_value=0.0)
    if len(trades):
        t = trades[trades["exit_time"].dt.normalize().isin(days)]
    else:
        t = trades
    return d, t
