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

import _bootstrap
from tradebot.bot.history import MAX_DAYS, download
from tradebot.config import DEFAULT_WATCHLIST


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

    download(kite, args.symbols, _bootstrap.ROOT / args.out, args.days, args.interval)

    print(f"\nSaved to {args.out}/. Now run: python scripts/run_backtest.py --data {args.out} --walk-forward")


if __name__ == "__main__":
    main()
