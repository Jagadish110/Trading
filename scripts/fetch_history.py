"""Download real 5-minute candles from Zerodha into data/nse/.

    python scripts/kite_login.py          # once per day
    python scripts/fetch_history.py --days 750

Needs the paid Kite Connect plan (the free Personal plan has no market
data). Only reads data; it never places orders.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

import pandas as pd

import _bootstrap
from tradebot.config import DEFAULT_WATCHLIST

# Longest span Kite returns in one request, by candle size.
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
            "15minute": 200, "30minute": 200, "60minute": 400}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_WATCHLIST, help="NSE trading symbols")
    ap.add_argument("--days", type=int, default=750, help="calendar days of history")
    ap.add_argument("--interval", default="5minute", choices=list(MAX_DAYS))
    ap.add_argument("--out", default="data/nse")
    args = ap.parse_args()

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        sys.exit("kiteconnect is not installed. Run: pip install -r requirements.txt")
    _bootstrap.load_env()
    if not _bootstrap.SESSION_FILE.exists():
        sys.exit("No session found. Run scripts/kite_login.py first.")
    kite = KiteConnect(api_key=_bootstrap.require_env("KITE_API_KEY"))
    kite.set_access_token(json.loads(_bootstrap.SESSION_FILE.read_text())["access_token"])

    tokens = {i["tradingsymbol"]: i["instrument_token"]
              for i in kite.instruments("NSE") if i.get("instrument_type") == "EQ"}
    out = _bootstrap.ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    end = datetime.now()
    start = end - timedelta(days=args.days)
    step = timedelta(days=MAX_DAYS[args.interval])

    for sym in args.symbols:
        if sym not in tokens:
            print(f"{sym:<12} not found on NSE, skipped")
            continue
        rows, cursor = [], start
        while cursor < end:
            upto = min(cursor + step, end)
            rows += kite.historical_data(tokens[sym], cursor, upto, args.interval)
            cursor = upto + timedelta(seconds=1)
            time.sleep(0.4)                    # stay under 3 requests a second
        if not rows:
            print(f"{sym:<12} no data returned")
            continue
        df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
        df.to_csv(out / f"{sym}.csv", index=False)
        print(f"{sym:<12} {len(df):>7,} candles  {df['date'].iloc[0]:%Y-%m-%d} to {df['date'].iloc[-1]:%Y-%m-%d}")

    print(f"\nSaved to {args.out}/. Now run: python scripts/run_backtest.py --data {args.out} --walk-forward")


if __name__ == "__main__":
    main()
