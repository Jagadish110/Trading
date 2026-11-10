"""The loops that feed the engine: a replay of saved candles, and the live
loop used for both paper and real trading."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .engine import TradingEngine
from .kite import ist_now


def replay(engine: TradingEngine, data: dict[str, pd.DataFrame], precompute: bool = True,
           pace: float = 0.0, should_stop=None) -> None:
    """Run saved candles through the engine, oldest first.

    precompute=True works out every signal once up front. That is far
    faster and gives the same result, because strategies cannot see ahead.
    precompute=False recomputes on every candle, exactly as live trading
    does. `pace` is a pause in seconds after each candle time, so the
    dashboard can be watched.
    """
    data = dict(sorted(data.items()))
    symbols = list(data)
    cols = {s: {c: df[c].tolist() for c in ("open", "high", "low", "close", "volume")}
            for s, df in data.items()}
    sig = None
    if precompute:
        sig = {}
        for s, df in data.items():
            out = engine.strategy.signals(df)
            sig[s] = {c: out[c].tolist() for c in out.columns}

    ts = np.concatenate([df.index.as_unit("ns").asi8 for df in data.values()])
    sym = np.concatenate([np.full(len(df), k) for k, df in enumerate(data.values())])
    row = np.concatenate([np.arange(len(df)) for df in data.values()])
    order = np.lexsort((sym, ts))                        # by time, then symbol
    last_ts = None
    for t, k, i in zip(ts[order].tolist(), sym[order].tolist(), row[order].tolist()):
        if t != last_ts:
            if should_stop is not None and should_stop():
                break
            if pace and last_ts is not None:
                time.sleep(pace)
            last_ts = t
        s = symbols[k]
        stamp = pd.Timestamp(t)
        c = cols[s]
        engine.on_open(s, stamp, c["open"][i])
        engine.on_range(s, stamp, c["open"][i], c["high"][i], c["low"][i])
        if sig is None:
            engine.on_candle(s, stamp, c["open"][i], c["high"][i], c["low"][i], c["close"][i], c["volume"][i])
        else:
            g = sig[s]
            engine.set_intent(s, stamp, int(g["side"][i]), g["stop"][i], g["target"][i],
                              bool(g["exit_long"][i]), bool(g["exit_short"][i]), close=c["close"][i])
    engine.finish()


class LiveRunner:
    """Polls Zerodha and drives the engine through one trading day.

    Just after each candle closes it fetches that candle for every symbol,
    lets the strategy decide, and acts at the current price. In between it
    checks the price of every open position every few seconds.
    """
    OPEN, CLOSE = 9 * 60 + 15, 15 * 60 + 30

    def __init__(self, engine: TradingEngine, feed, symbols: list[str], tick_seconds: float = 3.0,
                 candle_delay: float = 2.0, candle_patience: float = 45.0, warmup_days: int = 10,
                 now=ist_now, sleep=time.sleep):
        self.engine, self.feed, self.symbols = engine, feed, list(symbols)
        self.bar = timedelta(minutes=feed.bar_min)
        self.tick_seconds = tick_seconds
        self.candle_delay = timedelta(seconds=candle_delay)
        self.candle_patience = timedelta(seconds=candle_patience)
        self.warmup_days = warmup_days
        self.now, self.sleep = now, sleep
        self.stop_requested = False
        self._seen: dict[str, datetime] = {}             # latest candle given to the engine
        self._boundary: datetime | None = None           # candle close being collected
        self._waiting: set[str] = set()
        self._errors = 0
        self._summarised = False

    # ------------------------------------------------------------------
    def warm_up(self) -> None:
        now = self.now()
        start = now - timedelta(days=self.warmup_days)
        for s in self.symbols:
            candles = self.feed.candles(s, start, now)
            self.engine.warm_up(s, candles)
            if candles:
                self._seen[s] = candles[-1][0]
        self._boundary = self._floor(now)                # only act on candles that close from now on
        self.engine.log("info", f"Warmed up {len(self.symbols)} symbols with "
                                f"{self.warmup_days} days of candles")

    def _floor(self, now: datetime) -> datetime:
        minutes = (now.hour * 60 + now.minute) // self.feed.bar_min * self.feed.bar_min
        return now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)

    def step(self) -> str:
        """One pass of the loop. Returns "waiting", "trading" or "done"."""
        now = self.now()
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or minute < self.OPEN:
            return "waiting"
        if minute >= self.CLOSE and not self.engine.positions:
            if not self._summarised:
                self._summarised = True
                self._summary()
            return "done"

        boundary = self._floor(now)
        closes_a_candle = self.OPEN < boundary.hour * 60 + boundary.minute <= self.CLOSE
        if closes_a_candle and (self._boundary is None or boundary > self._boundary) \
                and now >= boundary + self.candle_delay:
            self._boundary, self._waiting = boundary, set(self.symbols)
        if self._waiting:
            if now > self._boundary + self.candle_patience:
                self.engine.log("warn", f"No {self._boundary:%H:%M} candle for: "
                                        f"{', '.join(sorted(self._waiting))}")
                self._waiting.clear()
            else:
                self._collect(now)

        held = list(self.engine.positions)
        if held:
            prices = self.feed.prices(held)
            stamp = pd.Timestamp(self.now())
            for s in held:
                if s in prices:
                    self.engine.on_tick(s, stamp, prices[s])
        return "trading"

    def _collect(self, now: datetime) -> None:
        """Fetch the candle that just closed and act on its signal."""
        want = self._boundary - self.bar                 # start time of the candle that closed
        fresh = []
        for s in sorted(self._waiting):
            since = self._seen.get(s, want - self.bar)
            candles = [c for c in self.feed.candles(s, since, now) if c[0] > since]
            for c in candles:
                self.engine.on_candle(s, pd.Timestamp(c[0]), *c[1:])
                self._seen[s] = c[0]
            if candles and candles[-1][0] >= want:
                fresh.append(s)
        if fresh:
            prices = self.feed.prices(fresh)
            stamp = pd.Timestamp(self.now())
            for s in fresh:
                if s in prices:
                    self.engine.on_open(s, stamp, prices[s])
            self._waiting -= set(fresh)

    def _summary(self) -> None:
        e = self.engine
        day = str(e._day)
        done = e.store.trades(e.mode, day)
        net = sum(t["net"] for t in done)
        wins = sum(t["net"] > 0 for t in done)
        e.log("info", f"Day over ({e.mode}): {len(done)} trades, {wins} winners, "
                      f"net {'-' if net < 0 else '+'}Rs {abs(net):,.0f}", alert=True)

    def run(self) -> None:
        self.warm_up()
        while not self.stop_requested:
            try:
                state = self.step()
                self._errors = 0
            except Exception as e:                       # network blips must not kill the bot
                self._errors += 1
                if self._errors in (1, 20):
                    self.engine.log("error", f"Loop error ({type(e).__name__}: {e}). "
                                             f"Retrying every {self.tick_seconds:g}s.",
                                    alert=self._errors == 20)
                state = "trading"
            if state == "done":
                break
            self.sleep(30 if state == "waiting" else self.tick_seconds)
