"""Loading candles in the format Kite's historical API returns."""
import pandas as pd

from tradebot.data import load_csv, load_folder, make_synthetic, save_folder


def test_kite_format_csv(tmp_path):
    (tmp_path / "INFY.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-01-05 09:00:00+05:30,99,99,99,99,10\n"       # pre-open: dropped
        "2026-01-05 09:20:00+05:30,101,102,100,101,200\n"
        "2026-01-05 09:15:00+05:30,100,101,99,100,100\n"   # out of order: sorted
        "2026-01-05 09:20:00+05:30,101,103,100,102,250\n"  # duplicate: last one wins
        "2026-01-05 15:30:00+05:30,105,105,105,105,10\n"   # after close: dropped
    )
    df = load_csv(tmp_path / "INFY.csv")
    assert df.index.tz is None
    assert list(df.index) == [pd.Timestamp("2026-01-05 09:15"), pd.Timestamp("2026-01-05 09:20")]
    assert df.loc["2026-01-05 09:20", "close"] == 102
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_synthetic_round_trip(tmp_path):
    data = make_synthetic(["AAA", "BBB"], days=3, seed=1)
    save_folder(data, tmp_path)
    back = load_folder(tmp_path, symbols=["aaa"])
    assert list(back) == ["AAA"]
    assert len(back["AAA"]) == 3 * 75
    df = back["AAA"]
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
