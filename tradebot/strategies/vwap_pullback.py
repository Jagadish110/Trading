"""VWAP pullback.

Idea: when price has spent most of the recent session on one side of VWAP,
a dip back to VWAP that gets rejected is a chance to join the trend.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import atr, bar_number, day_key, session_vwap
from .base import Strategy


class VwapPullback(Strategy):
    name = "vwap_pullback"
    default_params = {
        "lookback": 12,         # candles used to judge which side of VWAP price lives on
        "side_frac": 0.8,       # share of those candles that must be on that side
        "rr": 2.0,
        "stop_atr": 0.5,        # stop this many ATRs beyond the pullback candle / VWAP
        "atr_period": 14,
    }
    param_grid = {"lookback": [6, 12], "rr": [1.5, 2.0], "stop_atr": [0.5, 1.0]}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty(df)
        vwap = session_vwap(df)
        a = atr(df, p["atr_period"])
        barno = bar_number(df)
        need = p["lookback"] * p["side_frac"]

        above = (df["close"] > vwap).astype(float)
        below = (df["close"] < vwap).astype(float)
        # Count the previous `lookback` candles, all from today.
        enough_today = barno >= p["lookback"]
        was_above = (above.rolling(p["lookback"]).sum().shift(1) >= need) & enough_today
        was_below = (below.rolling(p["lookback"]).sum().shift(1) >= need) & enough_today

        long = was_above & (df["low"] <= vwap) & (df["close"] > vwap) & (df["close"] > df["open"])
        short = was_below & (df["high"] >= vwap) & (df["close"] < vwap) & (df["close"] < df["open"])
        day = day_key(df)
        long &= long.groupby(day).cumsum() <= 2          # at most two tries a day per side
        short &= short.groupby(day).cumsum() <= 2

        return self.fill(
            out, long, short, df["close"],
            long_stop=pd.concat([df["low"], vwap], axis=1).min(axis=1) - p["stop_atr"] * a,
            short_stop=pd.concat([df["high"], vwap], axis=1).max(axis=1) + p["stop_atr"] * a,
            rr=p["rr"],
        )
