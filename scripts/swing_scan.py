"""Run the swing scan once in the terminal, without Telegram.

    python scripts/swing_scan.py                  # download prices, then scan
    python scripts/swing_scan.py --demo           # fake prices, no internet needed
    python scripts/swing_scan.py --choose-again   # re-test the strategies now
"""
from __future__ import annotations

import argparse

import _bootstrap
from tradebot.select import Selection
from tradebot.swing.data import make_daily_synthetic, save_daily
from tradebot.swing.service import SwingService


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=20_000)
    ap.add_argument("--demo", action="store_true", help="use fake prices in data/swing_demo")
    ap.add_argument("--choose-again", action="store_true")
    args = ap.parse_args()

    folder = _bootstrap.ROOT / "data" / ("swing_demo" if args.demo else "swing")
    svc = SwingService(folder, args.capital)
    if args.demo:
        save_daily(make_daily_synthetic([f"FAKE{i:02d}" for i in range(1, 31)]), svc.prices)
        print("NOTE: fake random prices. This shows the scan runs; it says nothing about real stocks.\n")
    say = lambda m: print("  " + m, flush=True)
    text = svc.run(download=not args.demo, choose_again=args.choose_again, say=say)
    pick = Selection.load(svc.selection_path)
    if pick:
        print("\n" + pick.report())
    print("\n" + text + "\n")


if __name__ == "__main__":
    main()
