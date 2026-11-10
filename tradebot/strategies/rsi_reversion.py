"""RSI mean reversion.

Idea: after a stretched move away from VWAP, RSI turning back from an
extreme often leads to a drift back toward VWAP. The target is VWAP itself.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import atr, rsi, session_vwap
from .base import Strategy


class RsiReversion(Strategy):
    name = "rsi_reversion"
    default_params = {
        "period": 14,
        "lower": 25,            # oversold level; overbought is 100 - lower
        "atr_mult": 1.5,        # stop distance in ATRs
        "min_rr": 1.0,          # skip if VWAP is closer than this many times the risk
        "atr_period": 14,
    }
    param_grid = {"period": [7, 14], "lower": [20, 25, 30], "atr_mult": [1.0, 1.5]}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty(df)
        r = rsi(df["close"], p["period"])
        vwap = session_vwap(df)
        a = atr(df, p["atr_period"])
        upper = 100 - p["lower"]
        risk = p["atr_mult"] * a

        turn_up = (r.shift(1) < p["lower"]) & (r >= p["lower"])
        turn_down = (r.shift(1) > upper) & (r <= upper)
        long = turn_up & ((vwap - df["close"]) >= p["min_rr"] * risk)
        short = turn_down & ((df["close"] - vwap) >= p["min_rr"] * risk)

        return self.fill(
            out, long, short, df["close"],
            long_stop=df["close"] - risk,
            short_stop=df["close"] + risk,
            long_target=vwap,
            short_target=vwap,
        )
