"""Test every strategy on saved candles and say which one, if any, to trade.

    python scripts/choose_strategy.py --data data/nse
    python scripts/choose_strategy.py --data data/sample

This is what `run_bot.py` does by itself once a week. Run it by hand to
see the reasoning. It only reads candle files; it places no orders.
"""
from __future__ import annotations

import argparse
from dataclasses import replace

import _bootstrap
from tradebot.config import RiskConfig
from tradebot.data import load_folder
from tradebot.select import choose


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="folder of SYMBOL.csv candle files")
    ap.add_argument("--capital", type=float, default=RiskConfig.capital)
    ap.add_argument("--risk-pct", type=float, default=RiskConfig.risk_per_trade_pct)
    ap.add_argument("--no-short", action="store_true")
    args = ap.parse_args()
    risk = replace(RiskConfig(), capital=args.capital, risk_per_trade_pct=args.risk_pct)
    data = load_folder(_bootstrap.ROOT / args.data)
    pick = choose(data, risk, allow_short=not args.no_short,
                  progress=lambda m: print("  " + m, flush=True))
    print("\n" + pick.report() + "\n")


if __name__ == "__main__":
    main()
