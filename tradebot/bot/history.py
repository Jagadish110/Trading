"""Download candle history from Zerodha into a folder of SYMBOL.csv files."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# Longest span Kite returns in one request, by candle size.
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
            "15minute": 200, "30minute": 200, "60minute": 400}


def download(kite, symbols: list[str], out: str | Path, days: int = 750,
             interval: str = "5minute", end: datetime | None = None,
             sleep=time.sleep, say=print) -> list[str]:
    """Returns the symbols that were saved. Only reads data; never places orders."""
    tokens = {i["tradingsymbol"]: i["instrument_token"]
              for i in kite.instruments("NSE") if i.get("instrument_type") == "EQ"}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    end = end or datetime.now()
    start = end - timedelta(days=days)
    step = timedelta(days=MAX_DAYS[interval])
    saved = []
    for sym in symbols:
        if sym not in tokens:
            say(f"{sym:<12} not found on NSE, skipped")
            continue
        rows, cursor = [], start
        while cursor < end:
            upto = min(cursor + step, end)
            rows += kite.historical_data(tokens[sym], cursor, upto, interval)
            cursor = upto + timedelta(seconds=1)
            sleep(0.4)                         # stay under 3 requests a second
        if not rows:
            say(f"{sym:<12} no data returned")
            continue
        df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
        df.to_csv(out / f"{sym}.csv", index=False)
        saved.append(sym)
        say(f"{sym:<12} {len(df):>7,} candles  {df['date'].iloc[0]:%Y-%m-%d} to {df['date'].iloc[-1]:%Y-%m-%d}")
    return saved
