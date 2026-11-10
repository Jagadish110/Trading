"""Supertrend flip.

Idea: Supertrend is an ATR-based trailing line. When price flips it from
down to up, go long with the line as the stop, and exit if it flips back.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import supertrend
from .base import Strategy


class SupertrendFlip(Strategy):
    name = "supertrend"
    default_params = {"period": 10, "mult": 3.0, "rr": 2.0}
    param_grid = {"mult": [2.0, 3.0], "rr": [2.0, 3.0]}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty(df)
        st = supertrend(df, p["period"], p["mult"])
        d, prev = st["direction"], st["direction"].shift(1)

        flip_up = (d == 1) & (prev == -1)
        flip_down = (d == -1) & (prev == 1)
        out["exit_long"] = flip_down.to_numpy()
        out["exit_short"] = flip_up.to_numpy()
        return self.fill(
            out, flip_up, flip_down, df["close"],
            long_stop=st["line"], short_stop=st["line"], rr=p["rr"],
        )
