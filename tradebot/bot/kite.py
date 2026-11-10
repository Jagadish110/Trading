"""Zerodha Kite Connect: session and market data.

Prices are polled rather than streamed. A 5-minute strategy does not need
a websocket, and polling keeps the bot a single simple loop.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    """Current time in India, whatever the laptop's own time zone is."""
    return datetime.now(IST).replace(tzinfo=None)


def connect(api_key: str, session_file: str | Path):
    """A logged-in KiteConnect from the token scripts/kite_login.py saved."""
    from kiteconnect import KiteConnect

    session_file = Path(session_file)
    if not session_file.exists():
        raise RuntimeError("No Zerodha session found. Run: python scripts/kite_login.py")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(json.loads(session_file.read_text())["access_token"])
    try:
        kite.profile()
    except Exception as e:
        raise RuntimeError(f"Zerodha session is not valid ({e}). "
                           "Log in again: python scripts/kite_login.py") from e
    return kite


class KiteFeed:
    def __init__(self, kite, symbols: list[str], bar_min: int = 5, pause: float = 0.35,
                 sleep=time.sleep):
        self.kite = kite
        self.symbols = list(symbols)
        self.bar_min = bar_min
        self.interval = "minute" if bar_min == 1 else f"{bar_min}minute"
        self._pause, self._sleep = pause, sleep         # stay under 3 history requests a second
        listed = {i["tradingsymbol"]: i for i in kite.instruments("NSE")
                  if i.get("instrument_type") == "EQ"}
        missing = [s for s in self.symbols if s not in listed]
        if missing:
            raise RuntimeError(f"Not found on NSE: {', '.join(missing)}")
        self.tokens = {s: listed[s]["instrument_token"] for s in self.symbols}
        self.tick_sizes = {s: float(listed[s].get("tick_size") or 0.05) for s in self.symbols}

    def candles(self, symbol: str, start: datetime, now: datetime) -> list[tuple]:
        """Completed candles from `start` up to `now`, oldest first, as
        (time, open, high, low, close, volume) with times in IST."""
        rows = self.kite.historical_data(self.tokens[symbol], start, now, self.interval)
        self._sleep(self._pause)
        out = []
        for r in rows:
            ts = pd.Timestamp(r["date"])
            if ts.tzinfo is not None:
                ts = ts.tz_convert(IST).tz_localize(None)
            if ts + timedelta(minutes=self.bar_min) <= now:      # skip the candle still forming
                out.append((ts, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r["volume"])))
        return out

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        quotes = self.kite.ltp(*[f"NSE:{s}" for s in symbols])
        return {k.split(":", 1)[1]: float(v["last_price"]) for k, v in quotes.items()}
