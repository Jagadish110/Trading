"""Backtest the strategies and compare them.

    python scripts/run_backtest.py --data data/sample
    python scripts/run_backtest.py --data data/nse --walk-forward
    python scripts/run_backtest.py --data data/nse --strategy orb --capital 200000

Without --walk-forward each strategy runs once with its default settings.
With it, settings are tuned on past windows and judged on unseen ones,
which is the result to believe.
"""
from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

import _bootstrap
from tradebot.backtest import Backtester
from tradebot.config import CostConfig, RiskConfig
from tradebot.data import load_folder
from tradebot.metrics import summarize
from tradebot.strategies import REGISTRY
from tradebot.walkforward import verdict, walk_forward


def fmt_row(name: str, m: dict, extra: str = "") -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    if m["wiped"]:
        extra = f"ACCOUNT WIPED OUT  {extra}"
    return (
        f"{name:<15}{m['trades']:>7}{m['win_rate']:>7.1f}%{pf:>7}"
        f"{m['net_pnl']:>12,.0f}{m['return_pct']:>8.1f}%{m['max_dd_pct']:>7.1f}%"
        f"{m['sharpe']:>7.2f}{m['charges']:>10,.0f}  {extra}"
    )


HEADER = (
    f"{'strategy':<15}{'trades':>7}{'win':>8}{'PF':>7}{'net Rs':>12}{'return':>9}"
    f"{'maxDD':>8}{'sharpe':>7}{'costs':>10}"
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="folder of SYMBOL.csv candle files")
    ap.add_argument("--strategy", default="all", help=f"all, or one of: {', '.join(REGISTRY)}")
    ap.add_argument("--capital", type=float, default=RiskConfig.capital)
    ap.add_argument("--risk-pct", type=float, default=RiskConfig.risk_per_trade_pct)
    ap.add_argument("--slippage-bps", type=float, default=CostConfig.slippage_bps)
    ap.add_argument("--no-short", action="store_true", help="long trades only")
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--train", type=int, default=120, help="training window, trading days")
    ap.add_argument("--test", type=int, default=40, help="unseen window, trading days")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    names = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    for n in names:
        if n not in REGISTRY:
            ap.error(f"unknown strategy '{n}'")

    data_dir = Path(args.data) if Path(args.data).is_absolute() else _bootstrap.ROOT / args.data
    data = load_folder(data_dir)
    risk = replace(RiskConfig(), capital=args.capital, risk_per_trade_pct=args.risk_pct)
    costs = replace(CostConfig(), slippage_bps=args.slippage_bps)
    bt = Backtester(data, risk=risk, costs=costs, allow_short=not args.no_short)
    out = _bootstrap.ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{len(data)} symbols, {len(bt.days)} trading days "
          f"({bt.days[0].date()} to {bt.days[-1].date()}), capital Rs {args.capital:,.0f}, "
          f"risk {args.risk_pct}% per trade, slippage {args.slippage_bps} bps")
    if any(s.startswith("FAKE") for s in data):
        print("NOTE: this is synthetic sample data. The numbers below show the pipeline works,\n"
              "      they say nothing about whether a strategy makes money in the real market.")

    summary = []
    if not args.walk_forward:
        print(f"\nDefault settings, whole period\n{HEADER}")
        for n in names:
            t0 = time.time()
            res = bt.run(REGISTRY[n]())
            m = summarize(res.daily, res.trades, res.capital)
            print(fmt_row(n, m, f"({time.time() - t0:.1f}s)"))
            res.trades.to_csv(out / f"trades_{n}.csv", index=False)
            res.daily.rename_axis("date").to_csv(out / f"daily_{n}.csv")
            summary.append({"strategy": n, "mode": "default", **m})
    else:
        rows = []
        for n in names:
            t0 = time.time()
            wf = walk_forward(bt, REGISTRY[n], train_days=args.train, test_days=args.test)
            label, why = verdict(wf)
            rows.append((n, wf, label, why, time.time() - t0))
            wf.oos_trades.to_csv(out / f"wf_trades_{n}.csv", index=False)
            wf.oos_daily.rename_axis("date").to_csv(out / f"wf_daily_{n}.csv")
            wf.folds.to_csv(out / f"wf_folds_{n}.csv", index=False)
            summary.append({"strategy": n, "mode": "walk_forward_unseen", "verdict": label, **wf.oos})
            summary.append({"strategy": n, "mode": "best_in_sample", **wf.best_in_sample})

        print(f"\nUNSEEN data (settings chosen on earlier windows): the result to believe\n{HEADER}")
        for n, wf, label, why, secs in rows:
            print(fmt_row(n, wf.oos, f"{label}: {why}"))
        print(f"\nBest settings picked with hindsight (flattering, do not trust)\n{HEADER}")
        for n, wf, *_ in rows:
            print(fmt_row(n, wf.best_in_sample, str(wf.best_in_sample_params)))
        print("\nA big gap between the two tables means the strategy was curve-fitted.")

    pd.DataFrame(summary).to_csv(out / "summary.csv", index=False)
    print(f"\nTrade logs and summary saved in {args.out}/\n")


if __name__ == "__main__":
    main()
