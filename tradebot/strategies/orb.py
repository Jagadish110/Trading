"""Opening range breakout.

Idea: the first N minutes set a high and a low. A close outside that range,
on above-average volume, often continues in the same direction.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import bar_minutes, bar_number, day_key
from .base import Strategy


class OpeningRangeBreakout(Strategy):
    name = "orb"
    default_params = {
        "or_minutes": 15,       # length of the opening range
        "rr": 2.0,              # target = this many times the risk
        "stop_frac": 0.5,       # stop sits this fraction of the range back from the breakout edge
        "vol_mult": 1.0,        # breakout candle volume vs the 20-candle average
        "min_range_pct": 0.2,   # ignore days with a tiny range ...
        "max_range_pct": 2.0,   # ... or a huge one
    }
    param_grid = {"or_minutes": [15, 30], "rr": [1.5, 2.0, 3.0], "stop_frac": [0.5, 1.0]}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty(df)
        day = day_key(df)
        barno = bar_number(df)
        n_or = max(1, p["or_minutes"] // bar_minutes(df))
        in_range = barno < n_or

        or_high = df["high"].where(in_range).groupby(day).transform("max")
        or_low = df["low"].where(in_range).groupby(day).transform("min")
        width = or_high - or_low
        width_pct = width / or_low * 100
        tradable = (barno >= n_or) & width_pct.between(p["min_range_pct"], p["max_range_pct"])
        vol_ok = df["volume"] > p["vol_mult"] * df["volume"].rolling(20).mean().shift(1)

        up = tradable & vol_ok & (df["close"] > or_high)
        down = tradable & vol_ok & (df["close"] < or_low)
        # Only the first breakout in each direction, each day.
        up &= up.groupby(day).cumsum() == 1
        down &= down.groupby(day).cumsum() == 1

        return self.fill(
            out, up, down, df["close"],
            long_stop=or_high - p["stop_frac"] * width,
            short_stop=or_low + p["stop_frac"] * width,
            rr=p["rr"],
        )
