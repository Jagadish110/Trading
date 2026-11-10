"""Technical indicators.

Every function here is causal: the value at bar t uses bars up to t only.
tests/test_strategies.py checks this by truncating the data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def bar_minutes(df: pd.DataFrame) -> int:
    """Candle size in minutes, inferred from the index."""
    gaps = pd.Series(df.index).diff().dt.total_seconds().dropna()
    return max(1, int(gaps.median() // 60)) if len(gaps) else 5


def day_key(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.normalize()


def bar_number(df: pd.DataFrame) -> pd.Series:
    """0 for the first candle of each day, 1 for the second, ..."""
    return df.groupby(day_key(df)).cumcount()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume weighted average price, reset at the start of every day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = day_key(df)
    cum_pv = (typical * df["volume"]).groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum()
    return (cum_pv / cum_v.where(cum_v > 0)).rename("vwap")


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Returns columns `line` and `direction` (+1 uptrend, -1 downtrend, 0 warm-up)."""
    a = atr(df, period).to_numpy()
    high, low, close = (df[c].to_numpy() for c in ("high", "low", "close"))
    mid = (high + low) / 2
    upper, lower = mid + mult * a, mid - mult * a

    n = len(df)
    f_up, f_lo = np.full(n, np.nan), np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)
    started = False
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if not started:
            f_up[i], f_lo[i], direction[i] = upper[i], lower[i], 1
            started = True
            continue
        f_up[i] = upper[i] if (upper[i] < f_up[i - 1] or close[i - 1] > f_up[i - 1]) else f_up[i - 1]
        f_lo[i] = lower[i] if (lower[i] > f_lo[i - 1] or close[i - 1] < f_lo[i - 1]) else f_lo[i - 1]
        if close[i] > f_up[i - 1]:
            direction[i] = 1
        elif close[i] < f_lo[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    line = np.where(direction == 1, f_lo, np.where(direction == -1, f_up, np.nan))
    return pd.DataFrame({"line": line, "direction": direction}, index=df.index)
