"""The daily swing scan: refresh prices, pick the strategy, update the book
and say in plain words what to do tomorrow morning."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..select import MAX_AGE_DAYS, Selection, choose
from .book import SwingBacktester, SwingBook, day_inputs
from .config import MAX_HOLD_DAYS, SWING_UNIVERSE, DeliveryCosts, swing_risk
from .data import load_daily, refresh
from .strategies import SWING_REGISTRY

WHY = {"signal_exit": "the setup has ended", "time_exit": f"held {MAX_HOLD_DAYS} trading days",
       "stop": "stop-loss", "target": "target", "data_end": "data ended"}


def _rs(x: float) -> str:
    return f"{'-' if x < 0 else '+'}Rs {abs(x):,.0f}"


class SwingService:
    def __init__(self, folder: str | Path, capital: float = 20_000.0, symbols: list[str] | None = None,
                 refresh_data=refresh, today=date.today, registry: dict | None = None,
                 train_days: int = 250, test_days: int = 60):
        self.folder = Path(folder)
        self.prices = self.folder / "daily"
        self.state_path = self.folder / "swing_state.json"
        self.selection_path = self.folder / "swing_selection.json"
        self.risk = swing_risk(capital)
        self.costs = DeliveryCosts()
        self.symbols = symbols or SWING_UNIVERSE
        self.refresh_data, self.today = refresh_data, today
        self.registry = registry or SWING_REGISTRY
        self.train_days, self.test_days = train_days, test_days

    # -- saved state -------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, book: SwingBook, strategy: str | None, params: dict | None) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        state = {**book.state(), "strategy": strategy, "params": params}
        state["trades"] = state["trades"][-200:]
        self.state_path.write_text(json.dumps(state, indent=1, default=str))

    def _pick(self, data, force: bool, say) -> Selection:
        pick = Selection.load(self.selection_path)
        if pick and not force and 0 <= pick.age_days(self.today()) < MAX_AGE_DAYS:
            return pick
        pick = choose(data, registry=self.registry, backtester=SwingBacktester(data, self.risk, self.costs),
                      train_days=self.train_days, test_days=self.test_days, fold_min_trades=10,
                      pass_min_trades=40, today=self.today(), progress=say)
        pick.save(self.selection_path)
        return pick

    # -- the scan ------------------------------------------------------------
    def run(self, download: bool = True, choose_again: bool = False, say=None) -> str:
        notes = []
        if download:
            failed = self.refresh_data(self.symbols, self.prices, say=say)
            if failed:
                notes.append(f"Could not download: {', '.join(failed)}.")
        try:
            data = load_daily(self.prices)
        except FileNotFoundError:
            return "No price data could be downloaded, so there is nothing to scan. Try again later."
        try:
            pick = self._pick(data, choose_again, say)
        except ValueError as e:
            return f"Cannot test the swing strategies: {e}"

        state = self._load_state()
        book = SwingBook(self.risk, self.costs, state=state)
        name, params = pick.strategy, pick.params
        buys_allowed = name is not None
        if name is None:                                  # nothing trusted: only manage what is open
            name, params = state.get("strategy"), state.get("params")
        events, last = [], book.last_day
        if name is not None:
            inputs = day_inputs(data, self.registry[name](**(params or {})))
            days = [d for d in inputs if book.last_day is None or d > book.last_day]
            if book.last_day is None:
                days = days[-1:]                          # first run: start from the latest day only
            for d in days:
                bars, signals = inputs[d]
                if not buys_allowed:
                    signals = {s: (0, *v[1:]) for s, v in signals.items()}
                events = book.step(d, bars, signals)     # only the newest day's events are reported
            last = book.last_day
        self._save_state(book, name, params)
        return self._message(pick, book, events, last, notes, buys_allowed)

    def skip(self, symbol: str) -> str:
        """The user did not take (or already sold) this one: forget it."""
        symbol = symbol.upper()
        state = self._load_state()
        book = SwingBook(self.risk, self.costs, state=state)
        if symbol not in book.positions and symbol not in book.pending_buys:
            return f"{symbol} is not in the swing list."
        book.positions.pop(symbol, None)
        book.pending_buys.pop(symbol, None)
        book.pending_sells.pop(symbol, None)
        self._save_state(book, state.get("strategy"), state.get("params"))
        return f"{symbol} removed. Its result is not counted."

    def holdings(self) -> str:
        book = SwingBook(self.risk, self.costs, state=self._load_state())
        if not book.positions and not book.pending_buys:
            return "Swing: nothing held."
        lines = [f"Swing, as of {book.last_day}:"]
        lines += [f"• Holding {p['qty']} {s}, bought {p['entry_price']:.2f}, stop {p['stop']:.2f}, "
                  f"target {p['target']:.2f}" for s, p in book.positions.items()]
        lines += [f"• To buy next morning: {s}" for s in book.pending_buys]
        return "\n".join(lines)

    def _message(self, pick, book, events, last, notes, buys_allowed) -> str:
        buy = [e for e in events if e["kind"] == "buy_tomorrow"]
        sell = [e for e in events if e["kind"] == "sell_tomorrow"]
        sold = [e for e in events if e["kind"] == "sold"]
        bought = [e for e in events if e["kind"] == "bought"]
        skipped = [e for e in events if e["kind"] == "skipped"]
        out = [f"Swing scan, prices up to {last or pick.data_to}"]
        if buys_allowed:
            out.append(f"Strategy in use: {pick.strategy} (chosen {pick.chosen_on} by testing on unseen data)")
        else:
            out.append("No swing strategy passed the test on unseen data, so there are NO new buys. "
                       "This is re-tested every week.")
        if buy:
            out.append("\nBUY next trading morning (delivery / CNC):")
            for e in buy:
                out.append(f"• Buy {e['qty']} {e['symbol']} near {e['close']:.2f}. Straight after, set a GTT with "
                           f"stop-loss {e['stop']:.2f} and target {e['target']:.2f}. "
                           f"Risk about Rs {(e['close'] - e['stop']) * e['qty']:,.0f}. "
                           f"Do not buy if it opens above {e['target']:.2f} or below {e['stop']:.2f}.")
        if sell:
            out.append("\nSELL next trading morning:")
            out += [f"• Sell {e['qty']} {e['symbol']} at the open: {WHY[e['reason']]}. Then cancel its GTT."
                    for e in sell]
        if sold or bought or skipped:
            out.append("\nWhat happened on the last day:")
            out += [f"• {e['symbol']}: assumed bought, {e['qty']} at about {e['price']:.2f}." for e in bought]
            out += [f"• {e['symbol']}: buy cancelled, {e['why']}." for e in skipped]
            for e in sold:
                how = ("your GTT should have sold it; check" if e["reason"] in ("stop", "target")
                       else "sold at the open")
                out.append(f"• {e['symbol']}: {WHY.get(e['reason'], e['reason'])} at {e['exit_price']:.2f}, "
                           f"{how}. About {_rs(e['net'])}.")
        if book.positions:
            out.append("\nHolding:")
            out += [f"• {p['qty']} {s}: stop {p['stop']:.2f}, target {p['target']:.2f}, "
                    f"day {p['held']} of {MAX_HOLD_DAYS}" for s, p in book.positions.items()]
        if not (buy or sell):
            out.append("\nNothing to do tomorrow.")
        if buy or book.positions:
            out.append("\nIf you did not take a trade, send /skip SYMBOL so I stop tracking it.")
        out += notes
        return "\n".join(out)

