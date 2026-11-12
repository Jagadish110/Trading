"""Let the bot choose its own strategy.

Every strategy is put through the walk-forward test. One is chosen only if

  * it made money on data its settings had never seen (verdict PASS), and
  * it also made money over the most recent training window.

Among those, the one with the most profit per rupee of drawdown wins, and
it trades with the settings that were best over the most recent window.

If nothing qualifies the answer is "do not trade". That is a real result,
not a failure: it means none of the rules has an edge on this data.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from .backtest import Backtester
from .config import CostConfig, RiskConfig
from .strategies import REGISTRY
from .walkforward import verdict, walk_forward

MAX_AGE_DAYS = 7            # choose again after this many days


@dataclass
class Selection:
    chosen_on: str                      # date the choice was made
    data_from: str
    data_to: str
    symbols: int
    strategy: str | None                # None = nothing qualified, do not trade
    params: dict | None
    reason: str
    table: list[dict] = field(default_factory=list)     # one row per strategy tested

    def age_days(self, today: date) -> int:
        return (today - date.fromisoformat(self.chosen_on)).days

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2, default=str))

    @staticmethod
    def load(path: str | Path) -> "Selection | None":
        try:
            return Selection(**json.loads(Path(path).read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def report(self) -> str:
        lines = [f"Tested on {self.symbols} stocks, {self.data_from} to {self.data_to} "
                 "(results on data the settings had never seen):", "",
                 f"  {'strategy':<15}{'trades':>7}{'win':>7}{'PF':>6}{'net Rs':>12}{'maxDD':>8}  verdict"]
        for r in self.table:
            lines.append(f"  {r['strategy']:<15}{r['trades']:>7}{r['win_rate']:>6.1f}%{r['profit_factor']:>6.2f}"
                         f"{r['net_pnl']:>12,.0f}{r['max_dd_pct']:>7.1f}%  {r['verdict']}: {r['why']}")
        lines += ["", (f"CHOSEN: {self.strategy} {self.params}" if self.strategy else "CHOSEN: nothing. Do not trade."),
                  self.reason]
        return "\n".join(lines)


def choose(data: dict[str, pd.DataFrame], risk: RiskConfig | None = None,
           costs: CostConfig | None = None, allow_short: bool = True,
           train_days: int = 120, test_days: int = 40, registry: dict | None = None,
           today: date | None = None, progress=None, backtester=None,
           fold_min_trades: int = 30, pass_min_trades: int = 100) -> Selection:
    """`backtester` lets another engine (the swing one) be judged the same way."""
    registry = registry or REGISTRY
    bt = backtester or Backtester(data, risk=risk, costs=costs, allow_short=allow_short)
    table, candidates = [], []
    for name, cls in registry.items():
        if progress:
            progress(f"testing {name} ...")
        wf = walk_forward(bt, cls, train_days=train_days, test_days=test_days, min_trades=fold_min_trades)
        label, why = verdict(wf, min_trades=pass_min_trades)
        m = wf.oos
        if label == "PASS" and wf.next_params is None:
            label, why = "FAIL", "passed overall but lost money over the most recent months"
        pf = m["profit_factor"]
        table.append({"strategy": name, "verdict": label, "why": why, "trades": m["trades"],
                      "win_rate": m["win_rate"], "profit_factor": 99.0 if pf == float("inf") else pf,
                      "net_pnl": m["net_pnl"], "max_dd_pct": m["max_dd_pct"]})
        if label == "PASS":
            score = m["net_pnl"] / max(m["max_dd_rs"], bt.risk_cfg.capital * 0.005)
            candidates.append((score, name, wf.next_params))

    if candidates:
        score, name, params = max(candidates, key=lambda c: c[0])
        reason = (f"{name} passed the unseen-data test with the best profit for its drawdown. "
                  "This earns it a paper-trading trial. It is not a promise of profit.")
    else:
        name, params = None, None
        reason = ("No strategy made money on unseen data after costs, so none is trusted. "
                  "The bot will not trade until one does.")
    return Selection(
        chosen_on=str(today or date.today()),
        data_from=str(bt.days[0].date()), data_to=str(bt.days[-1].date()),
        symbols=len(data), strategy=name, params=params, reason=reason, table=table,
    )
