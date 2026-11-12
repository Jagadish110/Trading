"""Swing strategies on daily candles. Same interface as the intraday ones:
a signal is decided at a day's close and acted on at the next day's open.
Only buys are emitted."""
from __future__ import annotations

import pandas as pd

from ..indicators import atr, ema, rsi
from ..strategies.base import Strategy


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


class Breakout(Strategy):
    """A close above the highest high of the last N days, on strong volume,
    in a stock already above its long average."""
    name = "breakout"
    default_params = {"lookback": 20, "vol_mult": 1.5, "atr_mult": 2.0, "rr": 2.0, "trend": 100}
    param_grid = {"lookback": [20, 55], "atr_mult": [2.0, 3.0], "rr": [2.0, 3.0]}

    def signals(self, df):
        p, out = self.params, self.empty(df)
        prior_high = df["high"].rolling(p["lookback"]).max().shift(1)
        vol_ok = df["volume"] > p["vol_mult"] * df["volume"].rolling(20).mean().shift(1)
        up = (df["close"] > prior_high) & vol_ok & (df["close"] > _sma(df["close"], p["trend"]))
        up &= ~up.shift(1, fill_value=False)             # first day of the breakout only
        stop = df["close"] - p["atr_mult"] * atr(df, 14)
        return self.fill(out, up, up & False, df["close"], long_stop=stop, short_stop=stop, rr=p["rr"])


class Pullback(Strategy):
    """In an uptrend, a dip to the 20-day average that closes back above it."""
    name = "pullback"
    default_params = {"fast": 20, "slow": 50, "atr_mult": 1.5, "rr": 2.0}
    param_grid = {"slow": [50, 100], "atr_mult": [1.5, 2.5], "rr": [1.5, 2.5]}

    def signals(self, df):
        p, out = self.params, self.empty(df)
        fast, slow = ema(df["close"], p["fast"]), ema(df["close"], p["slow"])
        uptrend = (fast > slow) & (slow > slow.shift(10))
        bounce = (df["low"] <= fast) & (df["close"] > fast) & (df["close"] > df["open"])
        long = uptrend & bounce
        stop = df["close"] - p["atr_mult"] * atr(df, 14)
        out["exit_long"] = (df["close"] < slow).to_numpy()
        return self.fill(out, long, long & False, df["close"], long_stop=stop, short_stop=stop, rr=p["rr"])


class OversoldBounce(Strategy):
    """A sharp short-term dip in a stock that is still in a long-term
    uptrend. Sold when it closes back above its 5-day average."""
    name = "oversold_bounce"
    default_params = {"rsi_period": 3, "below": 15, "trend": 100, "atr_mult": 2.5}
    param_grid = {"rsi_period": [2, 3], "below": [10, 20], "atr_mult": [2.0, 3.0]}

    def signals(self, df):
        p, out = self.params, self.empty(df)
        a = atr(df, 14)
        long = (rsi(df["close"], p["rsi_period"]) < p["below"]) & (df["close"] > _sma(df["close"], p["trend"]))
        stop = df["close"] - p["atr_mult"] * a
        out["exit_long"] = (df["close"] > _sma(df["close"], 5)).to_numpy()
        return self.fill(out, long, long & False, df["close"], long_stop=stop, short_stop=stop,
                         long_target=df["close"] + 2 * p["atr_mult"] * a, short_target=stop)


SWING_REGISTRY = {cls.name: cls for cls in (Breakout, Pullback, OversoldBounce)}
