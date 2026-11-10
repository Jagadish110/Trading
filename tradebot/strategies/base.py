"""The one interface every strategy implements.

A strategy looks at candles and says what it would do at the close of each
one. It never sizes positions or places orders: the risk manager and the
executor do that. The same `signals()` call is used by the backtester now
and by paper and live trading later, so a strategy behaves identically in
all three.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class Strategy:
    name: str = "base"
    default_params: dict = {}
    param_grid: dict[str, list] = {}       # values the walk-forward test may choose from

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return one row per candle with these columns:

        side        +1 buy, -1 sell short, 0 nothing. Decided at the candle's
                    close; the engine enters at the NEXT candle's open.
        stop        stop-loss price for that entry
        target      profit-target price for that entry
        exit_long   True = close any open long at the next candle's open
        exit_short  True = close any open short at the next candle's open

        Must be causal: row t may only use candles up to and including t.
        """
        raise NotImplementedError

    @staticmethod
    def empty(df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "side": np.zeros(len(df), dtype=np.int8),
                "stop": np.nan,
                "target": np.nan,
                "exit_long": False,
                "exit_short": False,
            },
            index=df.index,
        )

    @staticmethod
    def fill(out: pd.DataFrame, long: pd.Series, short: pd.Series,
             entry: pd.Series, long_stop: pd.Series, short_stop: pd.Series,
             rr: float | None = None,
             long_target: pd.Series | None = None,
             short_target: pd.Series | None = None) -> pd.DataFrame:
        """Write entries into `out`. Targets come from `rr` (a multiple of the
        risk) unless explicit target prices are given."""
        long = long.fillna(False).to_numpy(dtype=bool)
        short = short.fillna(False).to_numpy(dtype=bool) & ~long
        if long_target is None:
            long_target = entry + rr * (entry - long_stop)
        if short_target is None:
            short_target = entry - rr * (short_stop - entry)
        out.loc[long, "side"] = 1
        out.loc[long, "stop"] = long_stop[long]
        out.loc[long, "target"] = long_target[long]
        out.loc[short, "side"] = -1
        out.loc[short, "stop"] = short_stop[short]
        out.loc[short, "target"] = short_target[short]
        return out

    def __repr__(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.params.items())})"
