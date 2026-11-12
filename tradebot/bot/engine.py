"""The trading engine: strategy -> risk manager -> broker.

It makes the same decisions in the same order as the backtester:

  * a signal is decided when a candle closes and acted on at the next price;
  * exits are handled before entries, and the stop before the target;
  * every entry must pass the RiskManager.

The one deliberate difference is the daily loss limit. Here it also counts
open positions, and when it is hit everything is closed (the backtester
only blocks new entries). Set `flatten_on_daily_loss=False` to switch that
off, which makes a replay match the backtester trade for trade.

Three calls drive it, and both the replay and the live loop use them:

    on_candle(symbol, ts, o, h, l, c, v)   a candle has closed
    on_open(symbol, ts, price)             first price after that candle
    on_range(symbol, ts, o, h, l)          prices seen since the last call
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..config import CostConfig, RiskConfig, SessionConfig
from ..costs import round_trip_charges
from ..risk import RiskManager
from ..strategies.base import Strategy
from .alerts import NullAlerts
from .broker import Broker, Position
from .store import Store


@dataclass(frozen=True)
class EngineConfig:
    history_bars: int = 1500            # candles kept per symbol for the indicators (about 20 days)
    flatten_on_daily_loss: bool = True  # close everything when the daily loss limit is hit
    allow_short: bool = True
    advise: bool = False                # assist mode: tell the user what to do, they place the orders


@dataclass
class Intent:
    """What the strategy said at the close of the latest candle."""
    day: object
    side: int
    stop: float
    target: float
    exit_long: bool
    exit_short: bool


def _minute(ts) -> int:
    return ts.hour * 60 + ts.minute


def _rupees(x: float) -> str:
    return f"{'-' if x < 0 else '+'}Rs {abs(x):,.0f}"


class TradingEngine:
    def __init__(self, strategy: Strategy, broker: Broker, store: Store | None = None,
                 risk: RiskConfig | None = None, session: SessionConfig | None = None,
                 costs: CostConfig | None = None, cfg: EngineConfig | None = None,
                 alerts=None, mode: str | None = None):
        self.strategy = strategy
        self.broker = broker
        self.mode = mode or broker.name                 # paper | live | replay
        self.store = store or Store()
        self.session = session or SessionConfig()
        self.costs = costs or CostConfig()
        self.cfg = cfg or EngineConfig()
        self.alerts = alerts or NullAlerts()
        self.risk = RiskManager(risk or RiskConfig())
        self.slip = self.costs.slippage_bps / 10_000

        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []                    # closed in this run
        self.advice: deque = deque(maxlen=20)           # assist mode: latest instructions, newest last
        self.skipped = {"risk_gate": 0, "bad_levels": 0, "too_small": 0, "shorts_off": 0, "rejected": 0}
        self.status = "running"                         # running | paused | stopped
        self.note = ""                                  # why it is paused, shown on the dashboard
        self.clock = None                               # time of the latest market data seen
        self.lock = threading.RLock()

        self._hist: dict[str, deque] = {}
        self._intent: dict[str, Intent] = {}
        self._last: dict[str, float] = {}
        self._day = None
        self._loss_tripped = False
        self._first = _minute(self.session.first_entry)
        self._last_entry = _minute(self.session.last_entry)
        self._square = _minute(self.session.square_off)

    # -- logging ---------------------------------------------------------
    def log(self, level: str, message: str, alert: bool = False) -> None:
        self.store.log(level, message, self.clock or datetime.now())
        if alert:
            self.alerts.send(message)

    # -- startup ---------------------------------------------------------
    def restore(self, today=None) -> None:
        """Pick up where a previous run left off: today's result so far and
        any position that was open when the bot stopped."""
        with self.lock:
            today = today or datetime.now().date()
            self.risk.total_pnl = float(self.store.totals(self.mode)["net"])
            done = self.store.trades(self.mode, str(today))
            self.risk.day_pnl = sum(t["net"] for t in done)
            self.risk.trades_today = sum(t["entry_time"][:10] == str(today) for t in done)
            self._day = today
            for row in self.store.open_positions(self.mode):
                if row["entry_time"][:10] != str(today):
                    self.store.delete_position(row["symbol"])
                    self.log("warn", f"{row['symbol']}: position from {row['entry_time'][:10]} was still "
                                     "open in the database. Intraday positions do not carry over, so it "
                                     "was dropped and its result is not recorded.", alert=True)
                    continue
                self.positions[row["symbol"]] = Position(
                    row["symbol"], row["side"], row["qty"], pd.Timestamp(row["entry_time"]),
                    row["entry_price"], row["stop"], row["target"], row["stop_order_id"])
                self.risk.trades_today += 1
                self.log("info", f"{row['symbol']}: resumed open position from the database")
            issues = self.broker.problems(list(self.positions.values()))
            if issues:
                self.pause("Positions at the broker do not match the bot's records: " + "; ".join(issues),
                           alert=True)

    # -- controls --------------------------------------------------------
    def pause(self, why: str = "Paused by you", alert: bool = False) -> None:
        """Stop opening new positions. Open ones keep their stops and targets."""
        with self.lock:
            self.risk.halted = True
            self.status, self.note = "paused", why
            self.log("warn", f"Paused: {why}", alert=alert)

    def resume(self) -> None:
        with self.lock:
            self.risk.halted = False
            self.status, self.note = "running", ""
            self.log("info", "Resumed")

    def flatten_all(self, reason: str = "manual") -> None:
        """Close every open position now, at the last price seen."""
        with self.lock:
            for sym in list(self.positions):
                self._close(sym, self._last.get(sym, self.positions[sym].entry_price), reason)

    def close_one(self, symbol: str) -> None:
        """Assist mode: the user says they have closed this position."""
        with self.lock:
            if symbol in self.positions:
                self._close(symbol, self._last.get(symbol, self.positions[symbol].entry_price), "manual")

    def skip(self, symbol: str) -> None:
        """Assist mode: the user did not take this trade. Forget it, record nothing."""
        with self.lock:
            if self.positions.pop(symbol, None) is None:
                return
            self.store.delete_position(symbol)
            self.risk.trades_today = max(0, self.risk.trades_today - 1)
            self.log("info", f"{symbol}: suggestion skipped by you, not counted")

    def _advise(self, symbol: str, kind: str, text: str) -> None:
        self.advice.append({"ts": str(self.clock), "symbol": symbol, "kind": kind, "text": text})
        self.log("trade", text, alert=True)

    def kill(self) -> None:
        """The big red button: no new entries, and close everything."""
        with self.lock:
            self.pause("Kill switch pressed", alert=True)
            self.flatten_all("manual")

    # -- market data in --------------------------------------------------
    def on_candle(self, symbol, ts, o, h, l, c, v) -> None:
        """A candle has closed: ask the strategy what it would do."""
        with self.lock:
            hist = self._hist.setdefault(symbol, deque(maxlen=self.cfg.history_bars))
            if hist and ts <= hist[-1][0]:
                return                                   # already seen
            hist.append((ts, o, h, l, c, v))
            df = pd.DataFrame(list(hist), columns=["ts", "open", "high", "low", "close", "volume"])
            row = self.strategy.signals(df.set_index("ts")).iloc[-1]
            self.set_intent(symbol, ts, int(row["side"]), float(row["stop"]), float(row["target"]),
                            bool(row["exit_long"]), bool(row["exit_short"]), close=c)

    def warm_up(self, symbol, candles) -> None:
        """Load earlier candles so indicators are ready. No signals are acted on."""
        with self.lock:
            hist = self._hist.setdefault(symbol, deque(maxlen=self.cfg.history_bars))
            for row in candles:
                if not hist or row[0] > hist[-1][0]:
                    hist.append(tuple(row))

    def set_intent(self, symbol, ts, side, stop, target, exit_long, exit_short, close=None) -> None:
        """Record a signal directly. `on_candle` calls this; a replay that has
        already computed every signal can call it instead of `on_candle`."""
        with self.lock:
            self._roll_day(ts)
            self._intent[symbol] = Intent(ts.date(), side, stop, target, exit_long, exit_short)
            if close is not None:
                self._last[symbol] = close
                self._check_daily_loss()

    def on_open(self, symbol, ts, price) -> None:
        """First price after a candle closed: act on that candle's signal."""
        with self.lock:
            self._roll_day(ts)
            self._last[symbol] = price
            intent = self._intent.pop(symbol, None)
            if intent is not None and intent.day != ts.date():
                intent = None
            m = _minute(ts)
            pos = self.positions.get(symbol)

            if pos is not None:
                if m >= self._square:
                    self._close(symbol, price, "square_off")
                elif intent and (intent.exit_long if pos.side == 1 else intent.exit_short):
                    self._close(symbol, price, "signal_exit")
                return                                   # never re-enter on the same price

            if intent is None or intent.side == 0 or not (self._first <= m <= self._last_entry):
                return
            want = intent.side
            if want == -1 and not self.cfg.allow_short:
                self.skipped["shorts_off"] += 1
            elif not self.risk.can_enter(len(self.positions)):
                self.skipped["risk_gate"] += 1
            else:
                px = price * (1 + self.slip * want)      # what the fill is expected to cost
                st, tg = intent.stop, intent.target
                if not ((st < px < tg) if want == 1 else (tg < px < st)):
                    self.skipped["bad_levels"] += 1       # gapped past the stop or target
                    return
                qty = self.risk.position_size(px, st)
                if qty < 1:
                    self.skipped["too_small"] += 1
                    return
                self._enter(symbol, ts, want, qty, price, st, tg)

    def on_range(self, symbol, ts, open_, high, low) -> None:
        """Prices seen since the last call. A single live price is passed as
        open = high = low; a replayed candle passes its real range."""
        with self.lock:
            self._roll_day(ts)
            self._last[symbol] = open_
            pos = self.positions.get(symbol)
            if pos is None:
                return
            try:
                stopped = self.broker.stop_fill(pos, open_, high, low)
            except Exception as e:
                self._broker_failure(f"{symbol}: {e}")
                return
            if stopped is not None:
                self._close(symbol, stopped, "stop", already_filled=True)
            elif _minute(ts) >= self._square:
                self._close(symbol, open_, "square_off")
            elif (high >= pos.target) if pos.side == 1 else (low <= pos.target):
                self._close(symbol, pos.target, "target")
            self._check_daily_loss()

    def on_tick(self, symbol, ts, price) -> None:
        self.on_range(symbol, ts, price, price, price)

    def finish(self) -> None:
        """End of a replay: anything still open is closed at the last price."""
        with self.lock:
            self._close_leftovers()
            self.status = "stopped"

    # -- internals -------------------------------------------------------
    def _roll_day(self, ts) -> None:
        day = ts.date()
        if day == self._day:
            self.clock = ts
            return
        if self._day is not None:
            self._close_leftovers()                      # stamped with the old day's last time
        self.clock = ts
        self._day = day
        self._intent.clear()
        self._loss_tripped = False
        self.risk.start_day()
        if self.status == "paused" and self.note.startswith("Daily loss limit"):
            self.resume()

    def _close_leftovers(self) -> None:
        """Positions still open when the day's data ends."""
        for sym in list(self.positions):
            if self.mode == "live":
                self.log("error", f"{sym}: still open after the session ended. Zerodha squares off "
                                  "intraday positions itself; check your account.", alert=True)
            self._close(sym, self._last.get(sym, self.positions[sym].entry_price), "data_end",
                        already_filled=True)

    def _enter(self, symbol, ts, side, qty, price, stop, target) -> None:
        try:
            fill = self.broker.enter(symbol, side, qty, price, stop)
        except Exception as e:
            self._broker_failure(f"{symbol}: entry failed: {e}")
            return
        if fill is None:
            self.skipped["rejected"] += 1
            return
        pos = Position(symbol, side, fill.qty, ts, fill.price, stop, target, fill.stop_order_id)
        self.positions[symbol] = pos
        self.risk.record_entry()
        self.store.save_position(self.mode, self.strategy.name, pos)
        if self.cfg.advise:
            verb, back = ("BUY", "SELL") if side == 1 else ("SELL SHORT", "BUY")
            self._advise(symbol, "enter",
                         f"{verb} {fill.qty} {symbol} now as an intraday order, near {price:.2f}. "
                         f"Straight after, place a stop-loss {back} order at {stop:.2f}. "
                         f"Take profit at {target:.2f}. If the stop is hit you lose about "
                         f"Rs {abs(fill.price - stop) * fill.qty:,.0f}.")
            return
        self.log("trade", f"{'BUY' if side == 1 else 'SELL SHORT'} {symbol} {fill.qty} @ {fill.price:.2f}"
                          f"  stop {stop:.2f}  target {target:.2f}", alert=True)

    def _close(self, symbol, price, reason, already_filled: bool = False) -> None:
        pos = self.positions[symbol]
        if not already_filled:
            try:
                price, reason = self.broker.exit(pos, price, reason)
            except Exception as e:
                self._broker_failure(f"{symbol}: exit failed: {e}")
                return
        del self.positions[symbol]
        buy_px, sell_px = (pos.entry_price, price) if pos.side == 1 else (price, pos.entry_price)
        gross = (sell_px - buy_px) * pos.qty
        charges = round_trip_charges(buy_px * pos.qty, sell_px * pos.qty, self.costs)
        net = gross - charges
        self.risk.record_exit(net)
        trade = {
            "symbol": symbol, "side": pos.side, "qty": pos.qty,
            "entry_time": pos.entry_time, "entry_price": pos.entry_price,
            "exit_time": self.clock, "exit_price": price, "stop": pos.stop, "target": pos.target,
            "reason": reason, "gross": gross, "charges": charges, "net": net,
        }
        self.trades.append(trade)
        self.store.add_trade(self.mode, self.strategy.name, trade)
        self.store.delete_position(symbol)
        if self.cfg.advise:
            back = "SELL" if pos.side == 1 else "BUY BACK"
            why = {"target": "target reached", "square_off": "end of day, close everything",
                   "signal_exit": "the setup has reversed", "daily_loss": "daily loss limit hit",
                   "manual": "closed by you", "data_end": "session ended"}.get(reason, reason)
            if reason == "stop":
                text = (f"{symbol}: price hit the stop at {pos.stop:.2f}. Your stop-loss order should have "
                        f"closed the {pos.qty} shares; check that it did. About {_rupees(net)}.")
            elif reason == "manual":
                text = f"{symbol}: marked closed by you near {price:.2f}. About {_rupees(net)}."
            else:
                text = (f"{back} {pos.qty} {symbol} now, near {price:.2f}: {why}. "
                        f"Then cancel its stop-loss order. About {_rupees(net)}.")
            self._advise(symbol, "exit", text)
            return
        self.log("trade", f"CLOSED {symbol} {pos.qty} @ {price:.2f} ({reason.replace('_', ' ')})  "
                          f"{_rupees(net)}  day {_rupees(self.risk.day_pnl)}", alert=True)

    def _broker_failure(self, message: str) -> None:
        """An order did not behave. Stop opening positions and shout (once)."""
        if self.note == message:
            return
        self.risk.halted = True
        self.status, self.note = "paused", message
        self.log("error", message, alert=True)

    def unrealised(self) -> float:
        return sum((self._last.get(s, p.entry_price) - p.entry_price) * p.side * p.qty
                   for s, p in self.positions.items())

    def _check_daily_loss(self) -> None:
        if not self.cfg.flatten_on_daily_loss or self._loss_tripped:
            return
        if self.risk.day_pnl + self.unrealised() > -self.risk.daily_loss_limit:
            return
        self._loss_tripped = True
        self.pause(f"Daily loss limit of Rs {self.risk.daily_loss_limit:,.0f} hit. "
                   "No more trades today.", alert=True)
        self.flatten_all("daily_loss")

    # -- for the dashboard -------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            day = str(self._day) if self._day else None
            positions = []
            for s, p in self.positions.items():
                last = self._last.get(s, p.entry_price)
                positions.append({
                    "symbol": s, "side": p.side, "qty": p.qty, "entry_time": str(p.entry_time),
                    "entry_price": p.entry_price, "last": last, "stop": p.stop, "target": p.target,
                    "pnl": (last - p.entry_price) * p.side * p.qty,
                })
            totals = self.store.totals(self.mode)
            return {
                "mode": self.mode,
                "advise": self.cfg.advise,
                "advice": list(self.advice)[::-1],
                "strategy": repr(self.strategy),
                "status": self.status,
                "note": self.note,
                "clock": str(self.clock) if self.clock is not None else None,
                "day": day,
                "capital": self.risk.cfg.capital,
                "day_pnl": self.risk.day_pnl,
                "unrealised": self.unrealised(),
                "daily_loss_limit": self.risk.daily_loss_limit,
                "trades_today": self.risk.trades_today,
                "max_trades_per_day": self.risk.cfg.max_trades_per_day,
                "max_open_positions": self.risk.cfg.max_open_positions,
                "positions": positions,
                "trades": self.store.trades(self.mode, day, limit=50) if day else [],
                "totals": totals,
                "daily": self.store.daily(self.mode)[-30:],
                "events": self.store.events(40),
            }
