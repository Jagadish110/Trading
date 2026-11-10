import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradebot.strategies.base import Strategy  # noqa: E402


def make_day(day: str, closes: list[float], start: str = "09:15", bar_min: int = 5,
             spread: float = 0.5) -> pd.DataFrame:
    """A day of candles where each candle opens at the previous close."""
    idx = pd.date_range(f"{day} {start}", periods=len(closes), freq=f"{bar_min}min")
    close = np.array(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close) + spread,
         "low": np.minimum(open_, close) - spread, "close": close, "volume": 1000.0},
        index=idx,
    )


class Scripted(Strategy):
    """Emits exactly the signals it is given: {timestamp: (side, stop, target)}."""
    name = "scripted"

    def __init__(self, plan: dict, exits: dict | None = None):
        super().__init__()
        self.plan, self.exits = plan, exits or {}

    def signals(self, df):
        out = self.empty(df)
        for ts, (side, stop, target) in self.plan.items():
            ts = pd.Timestamp(ts)
            if ts in out.index:
                out.loc[ts, ["side", "stop", "target"]] = [side, stop, target]
        for ts, col in self.exits.items():
            if pd.Timestamp(ts) in out.index:
                out.loc[pd.Timestamp(ts), col] = True
        out["side"] = out["side"].astype("int8")
        return out


@pytest.fixture
def flat_day():
    """75 candles at 100 from 09:15 to 15:25: nothing happens unless a test edits it."""
    return make_day("2026-01-05", [100.0] * 75)
