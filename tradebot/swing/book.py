"""The swing book: one set of rules used by both the backtest and the
daily live scan, so they cannot drift apart.

`step()` takes one finished trading day and does, in order:

  1. sells announced yesterday happen at today's open;
  2. buys announced yesterday happen at today's open, unless the price
     opened beyond the stop or the target;
  3. stops and targets are checked against today's range, stop first;
  4. positions whose setup has ended, or that have been held too long,
     are announced for sale tomorrow;
  5. new buy signals are announced for tomorrow, as far as there is room.
"""
from __future__ import annotations

import pandas as pd

from ..backtest import TRADE_COLUMNS, BacktestResult
from ..config import RiskConfig
from ..risk import RiskManager
from ..strategies.base import Strategy
from .config import MAX_HOLD_DAYS, DeliveryCosts, delivery_charges, swing_risk


class SwingBook:
    def __init__(self, risk: RiskConfig | None = None, costs: DeliveryCosts | None = None,
                 max_hold_days: int = MAX_HOLD_DAYS, state: dict | None = None):
        self.risk_cfg = risk or swing_risk()
        self.costs = costs or DeliveryCosts()
        self.max_hold_days = max_hold_days
        self.sizer = RiskManager(self.risk_cfg)
        self.slip = self.costs.slippage_bps / 10_000
        state = state or {}
        self.positions: dict[str, dict] = state.get("positions", {})
        self.pending_buys: dict[str, dict] = state.get("pending_buys", {})
        self.pending_sells: dict[str, str] = state.get("pending_sells", {})
        self.last_day: str | None = state.get("last_day")
        self.trades: list[dict] = state.get("trades", [])

    def state(self) -> dict:
        return {"positions": self.positions, "pending_buys": self.pending_buys,
                "pending_sells": self.pending_sells, "last_day": self.last_day, "trades": self.trades}

    # ------------------------------------------------------------------
    def _close(self, sym: str, day: str, price: float, reason: str, events: list) -> None:
        p = self.positions.pop(sym)
        self.pending_sells.pop(sym, None)
        gross = (price - p["entry_price"]) * p["qty"]
        charges = delivery_charges(p["entry_price"] * p["qty"], price * p["qty"], self.costs)
        trade = {"symbol": sym, "side": 1, "qty": p["qty"], "entry_time": p["entry_day"],
                 "entry_price": p["entry_price"], "exit_time": day, "exit_price": price,
                 "stop": p["stop"], "target": p["target"], "reason": reason,
                 "gross": gross, "charges": charges, "net": gross - charges}
        self.trades.append(trade)
        events.append({"kind": "sold", **trade})

    def step(self, day, bars: dict[str, tuple], signals: dict[str, tuple]) -> list[dict]:
        """bars: symbol -> (open, high, low, close) for `day`.
        signals: symbol -> (side, stop, target, exit_long) decided at `day`'s close."""
        day = str(day)[:10]
        events: list[dict] = []

        for sym, reason in list(self.pending_sells.items()):             # 1
            if sym in bars and sym in self.positions:
                self._close(sym, day, bars[sym][0] * (1 - self.slip), reason, events)

        bought_today = set()
        for sym, want in list(self.pending_buys.items()):                # 2
            del self.pending_buys[sym]
            if sym not in bars:
                continue
            px = bars[sym][0] * (1 + self.slip)
            if not (want["stop"] < px < want["target"]):
                events.append({"kind": "skipped", "symbol": sym, "why": "opened beyond the stop or target"})
                continue
            if len(self.positions) >= self.risk_cfg.max_open_positions:
                events.append({"kind": "skipped", "symbol": sym, "why": "no free slot"})
                continue
            qty = self.sizer.position_size(px, want["stop"])
            if qty < 1:
                events.append({"kind": "skipped", "symbol": sym, "why": "too expensive for the position limit"})
                continue
            self.positions[sym] = {"qty": qty, "entry_day": day, "entry_price": px,
                                   "stop": want["stop"], "target": want["target"], "held": 0}
            bought_today.add(sym)
            events.append({"kind": "bought", "symbol": sym, "qty": qty, "price": px})

        for sym in list(self.positions):                                 # 3
            if sym not in bars:
                continue
            p = self.positions[sym]
            o, h, l, _ = bars[sym]
            if o <= p["stop"] and sym not in bought_today:
                self._close(sym, day, o * (1 - self.slip), "stop", events)
            elif l <= p["stop"]:
                self._close(sym, day, p["stop"] * (1 - self.slip), "stop", events)
            elif h >= p["target"]:
                self._close(sym, day, p["target"], "target", events)
            else:
                p["held"] += 1

        for sym, p in self.positions.items():                            # 4
            if sym in self.pending_sells or sym not in bars:
                continue
            sig = signals.get(sym)
            if sig is not None and sig[3]:
                self.pending_sells[sym] = "signal_exit"
            elif p["held"] >= self.max_hold_days:
                self.pending_sells[sym] = "time_exit"
            if sym in self.pending_sells:
                events.append({"kind": "sell_tomorrow", "symbol": sym, "qty": p["qty"],
                               "reason": self.pending_sells[sym], "close": bars[sym][3]})

        room = self.risk_cfg.max_open_positions - (len(self.positions) - len(self.pending_sells))   # 5
        for sym in sorted(signals):
            if room < 1:
                break
            side, stop, target, _ = signals[sym]
            if side != 1 or sym in self.positions or sym not in bars:
                continue
            close = bars[sym][3]
            if not (stop < close < target):
                continue
            qty = self.sizer.position_size(close, stop)
            if qty < 1:
                continue
            self.pending_buys[sym] = {"stop": float(stop), "target": float(target)}
            events.append({"kind": "buy_tomorrow", "symbol": sym, "qty": qty, "close": close,
                           "stop": float(stop), "target": float(target)})
            room -= 1

        self.last_day = day
        return events


def day_inputs(data: dict[str, pd.DataFrame], strategy: Strategy):
    """Per-day bars and signals for every symbol: {day: ({sym: bar}, {sym: signal})}."""
    out: dict[str, tuple[dict, dict]] = {}
    for sym, df in sorted(data.items()):
        sig = strategy.signals(df)
        rows = zip(df.index.strftime("%Y-%m-%d"), df["open"].tolist(), df["high"].tolist(),
                   df["low"].tolist(), df["close"].tolist(), sig["side"].tolist(),
                   sig["stop"].tolist(), sig["target"].tolist(), sig["exit_long"].tolist())
        for day, o, h, l, c, side, stop, target, exit_long in rows:
            bars, signals = out.setdefault(day, ({}, {}))
            bars[sym] = (o, h, l, c)
            signals[sym] = (side, stop, target, exit_long)
    return dict(sorted(out.items()))


class SwingBacktester:
    """Same shape as the intraday Backtester, so walk-forward validation and
    the strategy chooser work on it unchanged."""

    def __init__(self, data: dict[str, pd.DataFrame], risk: RiskConfig | None = None,
                 costs: DeliveryCosts | None = None, max_hold_days: int = MAX_HOLD_DAYS):
        self.data = dict(sorted(data.items()))
        self.risk_cfg = risk or swing_risk()
        self.costs = costs or DeliveryCosts()
        self.max_hold_days = max_hold_days
        self.days = pd.DatetimeIndex(sorted(set().union(*(df.index.normalize() for df in self.data.values()))))

    def run(self, strategy: Strategy) -> BacktestResult:
        book = SwingBook(self.risk_cfg, self.costs, self.max_hold_days)
        inputs = day_inputs(self.data, strategy)
        last_close: dict[str, float] = {}
        for day, (bars, signals) in inputs.items():
            book.step(day, bars, signals)
            last_close.update({s: b[3] for s, b in bars.items()})
        for sym in list(book.positions):                 # still open when the data ends
            book._close(sym, book.last_day, last_close[sym], "data_end", [])
        trades = pd.DataFrame(book.trades, columns=TRADE_COLUMNS)
        daily = pd.Series(0.0, index=self.days, name="net")
        if len(trades):
            trades["entry_time"] = pd.to_datetime(trades["entry_time"])
            trades["exit_time"] = pd.to_datetime(trades["exit_time"])
            daily = daily.add(trades.groupby("exit_time")["net"].sum(), fill_value=0.0)
        return BacktestResult(strategy.name, dict(strategy.params), trades, daily, self.risk_cfg.capital)
