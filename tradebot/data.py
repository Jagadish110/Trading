"""Candle data: loading real CSVs and generating synthetic samples."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]
SESSION_START, SESSION_END = "09:15", "15:29"


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load one symbol's candles.

    Expects the columns Kite's historical API returns: date, open, high, low,
    close, volume. Timestamps are treated as IST and are candle start times.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = "date" if "date" in df.columns else "datetime"
    ts = pd.to_datetime(df[time_col])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.assign(ts=ts).set_index("ts")[COLUMNS].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.between_time(SESSION_START, SESSION_END)
    return df.dropna(subset=["open", "high", "low", "close"])


def load_folder(folder: str | Path, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Load every `SYMBOL.csv` in a folder (or only the listed symbols)."""
    folder = Path(folder)
    paths = sorted(folder.glob("*.csv"))
    if symbols:
        wanted = {s.upper() for s in symbols}
        paths = [p for p in paths if p.stem.upper() in wanted]
    data = {p.stem.upper(): load_csv(p) for p in paths}
    data = {s: df for s, df in data.items() if len(df)}
    if not data:
        raise FileNotFoundError(f"No candle CSVs found in {folder}")
    return data


def make_synthetic(
    symbols: list[str],
    days: int = 300,
    bar_min: int = 5,
    seed: int = 7,
    end: str = "2026-10-30",
) -> dict[str, pd.DataFrame]:
    """Fake candles that look roughly like intraday stock data.

    This exists only so the pipeline can be run and tested without a broker
    account. Results on it say NOTHING about real profitability.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=days)
    bars = (6 * 60 + 15) // bar_min                      # 09:15 to 15:30
    offsets = pd.to_timedelta(9 * 60 + 15 + bar_min * np.arange(bars), unit="m")
    # Volatility and volume are both higher near the open and the close.
    x = np.linspace(-1, 1, bars)
    u_shape = 0.6 + 0.9 * x**2

    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        price = float(rng.uniform(300, 3000))
        daily_vol = float(rng.uniform(0.010, 0.020))
        base_volume = float(rng.uniform(20_000, 200_000))
        frames = []
        for d in dates:
            price *= 1 + rng.normal(0, 0.005)             # overnight gap
            sigma = daily_vol / np.sqrt(bars) * u_shape
            noise = rng.normal(0, 1, bars) * sigma
            if rng.random() < 0.3:                        # trend day: steady drift
                rets = noise + rng.choice([-1, 1]) * daily_vol * rng.uniform(0.5, 1.2) / bars
            else:                                         # range day: moves partly reverse
                rets = noise - 0.25 * np.concatenate([[0.0], noise[:-1]])
            close = price * np.cumprod(1 + rets)
            open_ = np.concatenate([[price], close[:-1]])
            wick = np.abs(rng.normal(0, 1, (2, bars))) * sigma * 0.5
            high = np.maximum(open_, close) * (1 + wick[0])
            low = np.minimum(open_, close) * (1 - wick[1])
            volume = base_volume * u_shape * rng.lognormal(0, 0.4, bars)
            frames.append(
                pd.DataFrame(
                    {"open": open_, "high": high, "low": low, "close": close, "volume": volume.round()},
                    index=d + offsets,
                )
            )
            price = float(close[-1])
        df = pd.concat(frames)
        df[["open", "high", "low", "close"]] = (df[["open", "high", "low", "close"]] / 0.05).round() * 0.05
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        df.index.name = "date"
        out[sym] = df
    return out


def save_folder(data: dict[str, pd.DataFrame], folder: str | Path) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for sym, df in data.items():
        df.rename_axis("date").to_csv(folder / f"{sym}.csv", float_format="%.2f")
