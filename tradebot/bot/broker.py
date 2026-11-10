"""Brokers: where an approved trade actually gets filled.

PaperBroker simulates fills with the backtester's rules, so paper trading
and the backtest agree. KiteBroker places real orders at Zerodha and keeps
every position's stop-loss resting at the exchange.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..config import CostConfig

# Exit reasons that are market orders and so pay slippage in the simulation.
SLIPPING_EXITS = {"square_off", "signal_exit", "daily_loss", "manual"}


@dataclass
class Position:
    symbol: str
    side: int               # +1 long, -1 short
    qty: int
    entry_time: object
    entry_price: float
    stop: float
    target: float
    stop_order_id: str | None = None


@dataclass
class Fill:
    qty: int
    price: float
    stop_order_id: str | None = None


class Broker:
    name = "base"

    def enter(self, symbol: str, side: int, qty: int, price: float, stop: float) -> Fill | None:
        """Open a position near `price`. Returns None if nothing was filled."""
        raise NotImplementedError

    def exit(self, pos: Position, price: float, reason: str) -> tuple[float, str]:
        """Close a position. Returns the fill price and the final reason
        (which becomes "stop" if the stop-loss got there first)."""
        raise NotImplementedError

    def stop_fill(self, pos: Position, open_: float, high: float, low: float) -> float | None:
        """The price the stop-loss filled at, or None if it has not been hit."""
        raise NotImplementedError

    def problems(self, positions: list[Position]) -> list[str]:
        """Differences between what the bot believes and what the broker holds."""
        return []


class PaperBroker(Broker):
    """No orders leave the laptop. Fills follow the backtester's rules."""
    name = "paper"

    def __init__(self, costs: CostConfig | None = None):
        self.slip = (costs or CostConfig()).slippage_bps / 10_000

    def enter(self, symbol, side, qty, price, stop):
        return Fill(qty, price * (1 + self.slip * side))

    def exit(self, pos, price, reason):
        if reason in SLIPPING_EXITS:
            price = price * (1 - self.slip * pos.side)
        return price, reason

    def stop_fill(self, pos, open_, high, low):
        if pos.side == 1:
            if open_ <= pos.stop:                       # gapped through: the worse open price
                return open_ * (1 - self.slip)
            if low <= pos.stop:
                return pos.stop * (1 - self.slip)
        else:
            if open_ >= pos.stop:
                return open_ * (1 + self.slip)
            if high >= pos.stop:
                return pos.stop * (1 + self.slip)
        return None


class OrderError(RuntimeError):
    pass


class KiteBroker(Broker):
    """Real orders through Kite Connect, intraday (MIS) on NSE.

    Entry   a limit order a little through the market, cancelled if it has
            not filled within `fill_timeout` seconds.
    Stop    an SL-M order placed at the exchange the moment the entry fills.
            It stays active even if the laptop sleeps or loses internet.
            If it cannot be placed, the position is closed at once.
    Exit    target, square-off and signal exits cancel the stop order and
            then send a market order.

    `kite` is a logged-in kiteconnect.KiteConnect (or anything with the same
    methods, which is how the tests drive this class).
    """
    name = "live"
    DONE = {"COMPLETE", "REJECTED", "CANCELLED"}

    def __init__(self, kite, tick_sizes: dict[str, float] | None = None,
                 entry_buffer_pct: float = 0.1, fill_timeout: float = 10.0,
                 poll_seconds: float = 0.5, tag: str = "tradebot",
                 sleep=time.sleep, log=None):
        self.kite = kite
        self.tick_sizes = tick_sizes or {}
        self.entry_buffer = entry_buffer_pct / 100
        self.fill_timeout = fill_timeout
        self.poll_seconds = poll_seconds
        self.tag = tag
        self._sleep = sleep
        self._log = log or (lambda level, msg: None)

    # -- helpers ---------------------------------------------------------
    def _tick(self, symbol: str, price: float, up: bool | None = None) -> float:
        tick = self.tick_sizes.get(symbol) or 0.05
        n = price / tick
        n = round(n) if up is None else (math.ceil(n - 1e-9) if up else math.floor(n + 1e-9))
        return round(n * tick, 4)

    def _place(self, symbol: str, side: int, qty: int, order_type: str, **kw) -> str:
        return str(self.kite.place_order(
            variety="regular", exchange="NSE", tradingsymbol=symbol,
            transaction_type="BUY" if side == 1 else "SELL", quantity=int(qty),
            product="MIS", order_type=order_type, validity="DAY", tag=self.tag, **kw))

    def _state(self, order_id: str) -> dict:
        history = self.kite.order_history(order_id)
        return history[-1] if history else {"status": "UNKNOWN", "filled_quantity": 0}

    def _await(self, order_id: str, timeout: float) -> dict:
        """Poll until the order is finished or `timeout` seconds have passed."""
        waited, state = 0.0, self._state(order_id)
        while state.get("status") not in self.DONE and waited < timeout:
            self._sleep(self.poll_seconds)
            waited += self.poll_seconds
            state = self._state(order_id)
        return state

    def _cancel(self, order_id: str) -> dict:
        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
        except Exception as e:                           # usually: already filled
            self._log("warn", f"cancel of order {order_id} failed: {e}")
        return self._await(order_id, 3.0)

    def _market_out(self, symbol: str, side: int, qty: int) -> float:
        """Market order closing `qty` of a position on `side`. Returns the fill price."""
        oid = self._place(symbol, -side, qty, "MARKET", market_protection=-1)
        state = self._await(oid, self.fill_timeout)
        if state.get("status") != "COMPLETE":
            raise OrderError(f"{symbol}: exit order {oid} is {state.get('status')} "
                             f"({state.get('status_message') or 'no message'}). CHECK ZERODHA NOW.")
        return float(state["average_price"])

    # -- Broker interface -------------------------------------------------
    def enter(self, symbol, side, qty, price, stop):
        limit = self._tick(symbol, price * (1 + self.entry_buffer * side), up=side == 1)
        try:
            oid = self._place(symbol, side, qty, "LIMIT", price=limit)
        except Exception as e:
            self._log("error", f"{symbol}: entry order refused: {e}")
            return None
        state = self._await(oid, self.fill_timeout)
        if state.get("status") not in self.DONE:
            state = self._cancel(oid)                    # unfilled part is withdrawn
        filled = int(state.get("filled_quantity") or 0)
        if filled < 1:
            self._log("warn", f"{symbol}: entry not filled ({state.get('status')} "
                              f"{state.get('status_message') or ''})".rstrip())
            return None
        avg = float(state["average_price"])

        try:
            stop_id = self._place(symbol, -side, filled, "SL-M",
                                  trigger_price=self._tick(symbol, stop), market_protection=-1)
            placed = self._state(stop_id)
            if placed.get("status") in ("REJECTED", "CANCELLED"):
                raise OrderError(placed.get("status_message") or placed["status"])
        except Exception as e:
            # Never hold a position without a stop at the exchange.
            self._log("error", f"{symbol}: stop-loss order failed ({e}); closing the position")
            self._market_out(symbol, side, filled)
            return None
        return Fill(filled, avg, stop_id)

    def stop_fill(self, pos, open_, high, low):
        if not pos.stop_order_id:
            return None
        state = self._state(pos.stop_order_id)
        if state.get("status") == "COMPLETE":
            return float(state["average_price"])
        if state.get("status") in ("REJECTED", "CANCELLED"):
            raise OrderError(f"{pos.symbol}: stop-loss order {pos.stop_order_id} is "
                             f"{state['status']} but the position is open. CHECK ZERODHA NOW.")
        return None

    def exit(self, pos, price, reason):
        if pos.stop_order_id:
            state = self._cancel(pos.stop_order_id)
            if state.get("status") == "COMPLETE":        # the stop got there first
                return float(state["average_price"]), "stop"
            left = pos.qty - int(state.get("filled_quantity") or 0)
        else:
            left = pos.qty
        return self._market_out(pos.symbol, pos.side, left), reason

    def problems(self, positions):
        held = {p["tradingsymbol"]: int(p["quantity"])
                for p in self.kite.positions().get("net", [])
                if p.get("product") == "MIS" and p.get("exchange") == "NSE" and int(p["quantity"]) != 0}
        mine = {p.symbol: p.side * p.qty for p in positions}
        out = []
        for sym in sorted(set(held) | set(mine)):
            if held.get(sym, 0) != mine.get(sym, 0):
                out.append(f"{sym}: bot expects {mine.get(sym, 0)} shares, Zerodha shows {held.get(sym, 0)}")
        return out
