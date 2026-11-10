"""EMA crossover trend following.

Idea: a fast average crossing a slow one, on the right side of VWAP, marks
the start of a move. The position is closed if the averages cross back.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import atr, ema, session_vwap
from .base import Strategy


class EmaTrend(Strategy):
    name = "ema_trend"
    default_params = {"fast": 9, "slow": 21, "atr_mult": 1.5, "rr": 2.0, "atr_period": 14}
    param_grid = {"fast": [9, 13], "slow": [21, 34], "atr_mult": [1.5, 2.0], "rr": [2.0, 3.0]}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty(df)
        fast, slow = ema(df["close"], p["fast"]), ema(df["close"], p["slow"])
        vwap = session_vwap(df)
        a = atr(df, p["atr_period"])

        ready = slow.notna() & slow.shift(1).notna()
        cross_up = ready & (fast > slow) & (fast.shift(1) <= slow.shift(1))
        cross_down = ready & (fast < slow) & (fast.shift(1) >= slow.shift(1))

        out["exit_long"] = cross_down.to_numpy()
        out["exit_short"] = cross_up.to_numpy()
        return self.fill(
            out,
            cross_up & (df["close"] > vwap),
            cross_down & (df["close"] < vwap),
            df["close"],
            long_stop=df["close"] - p["atr_mult"] * a,
            short_stop=df["close"] + p["atr_mult"] * a,
            rr=p["rr"],
        )
