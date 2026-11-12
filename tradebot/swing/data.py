"""Daily candles: free download from Yahoo Finance, CSV storage, fake data.

Yahoo's chart address is unofficial. It is free and needs no account, but
it can change or refuse requests without notice, and its prices are not
guaranteed. Good enough to scan with; check a price in your broker's app
before acting on it.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ..bot.kite import ist_now

COLUMNS = ["open", "high", "low", "close", "volume"]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?range={years}y&interval=1d"
MARKET_DATA_FINAL = (15, 45)             # today's candle is only complete after this time (IST)


def parse_yahoo(payload: dict) -> pd.DataFrame:
    """Yahoo chart JSON -> daily candles indexed by date, adjusted for splits and bonuses."""
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    stamps = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame({c: quote[c] for c in COLUMNS}, index=stamps.tz_localize(None).normalize()).astype(float)
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    if adj:
        factor = pd.Series(adj, index=df.index, dtype=float) / df["close"]
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] * factor
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "date"
    return df


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fetch_yahoo(symbol: str, years: int = 3, get=_http_get) -> pd.DataFrame:
    url = YAHOO.format(symbol=urllib.parse.quote(symbol, safe=""), years=years)
    return parse_yahoo(json.loads(get(url)))


def drop_unfinished(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Remove today's candle if the market has not finished yet."""
    if len(df) and df.index[-1].date() == now.date() and (now.hour, now.minute) < MARKET_DATA_FINAL:
        return df.iloc[:-1]
    return df


def refresh(symbols: list[str], folder: str | Path, years: int = 3, say=None, get=_http_get,
            now=ist_now, pause: float = 0.3, sleep=time.sleep) -> list[str]:
    """Download every symbol into `folder`. Returns the ones that failed."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    failed = []
    for sym in symbols:
        try:
            df = drop_unfinished(fetch_yahoo(sym, years, get), now())
            if len(df) < 50:
                raise ValueError("too little history")
            df.to_csv(folder / f"{sym}.csv", float_format="%.4f")
        except Exception as e:
            failed.append(sym)
            if say:
                say(f"{sym}: download failed ({type(e).__name__})")
        sleep(pause)
    return failed


def load_daily(folder: str | Path, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    for path in sorted(Path(folder).glob("*.csv")):
        if symbols and path.stem not in symbols:
            continue
        df = pd.read_csv(path, index_col="date", parse_dates=True)[COLUMNS].astype(float)
        if len(df):
            out[path.stem] = df.sort_index()
    if not out:
        raise FileNotFoundError(f"No daily candle files in {folder}")
    return out


def make_daily_synthetic(symbols: list[str], days: int = 700, seed: int = 11,
                         end: str = "2026-10-30") -> dict[str, pd.DataFrame]:
    """Fake daily candles (a random walk). For tests and demos only."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=days, name="date")
    out = {}
    for sym in symbols:
        close = float(rng.uniform(200, 1500)) * np.cumprod(1 + rng.normal(0.0003, 0.015, days))
        open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.004, days))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, days)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, days)))
        out[sym] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                                 "volume": rng.lognormal(12, 0.5, days).round()}, index=idx)
    return out


def save_daily(data: dict[str, pd.DataFrame], folder: str | Path) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for sym, df in data.items():
        df.rename_axis("date").to_csv(folder / f"{sym}.csv", float_format="%.4f")
