"""Generate synthetic candles so the backtester can be tried without a broker account.

    python scripts/make_sample_data.py

The data is fake. Use it to check that everything runs, not to judge strategies.
"""
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from tradebot.data import make_synthetic, save_folder


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data/sample")
    ap.add_argument("--symbols", type=int, default=10)
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    names = [f"FAKE{i:02d}" for i in range(1, args.symbols + 1)]
    data = make_synthetic(names, days=args.days, seed=args.seed)
    save_folder(data, _bootstrap.ROOT / args.out)
    bars = sum(len(df) for df in data.values())
    print(f"Wrote {len(data)} symbols, {args.days} days, {bars:,} candles to {args.out}/")


if __name__ == "__main__":
    main()
