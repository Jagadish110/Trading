"""Event-driven intraday backtester.

Rules that keep it honest:
  * A signal is decided at a candle's close and filled at the NEXT candle's
    open, never at the price that triggered it.
  * If a candle touches both the stop and the target, the stop is assumed
    to have been hit first.
  * A gap through the stop fills at the (worse) open price.
  * Entries, stop exits and square-offs pay slippage; every trade pays the
    full set of brokerage, taxes and exchange charges.
  * Everything is flat by the square-off time. Nothing is held overnight.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig, SessionConfig
from .costs import round_trip_charges
from .risk import RiskManager
from .strategies.base import Strategy

TRADE_COLUMNS = [
    "symbol", "side", "qty", "entry_time", "entry_price", "exit_time", "exit_price",
    "stop", "target", "reason", "gross", "charges", "net",
]


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    trades: pd.DataFrame
    daily: pd.Series            # net rupees per trading day (0 on days with no trades)
    capital: float
    skipped: dict = field(default_factory=dict)


class Backtester:
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        risk: RiskConfig | None = None,
        session: SessionConfig | None = None,
        costs: CostConfig | None = None,
        allow_short: bool = True,
    ):
        self.data = dict(sorted(data.items()))
        self.risk_cfg = risk or RiskConfig()
        self.session = session or SessionConfig()
        self.costs = costs or CostConfig()
        self.allow_short = allow_short
        self.symbols = list(self.data)

        # Per-symbol columns as plain lists: much faster to index in the loop.
        self._o, self._h, self._l, self._c = [], [], [], []
        self._day, self._minute = [], []
        ts_all, sym_all, row_all = [], [], []
        for s, sym in enumerate(self.symbols):
            df = self.data[sym]
            idx = df.index
            self._o.append(df["open"].tolist())
            self._h.append(df["high"].tolist())
            self._l.append(df["low"].tolist())
            self._c.append(df["close"].tolist())
            ns = idx.as_unit("ns")
            self._day.append((ns.normalize().asi8 // 86_400_000_000_000).tolist())
            self._minute.append((idx.hour * 60 + idx.minute).tolist())
            ts_all.append(ns.asi8)
            sym_all.append(np.full(len(df), s))
            row_all.append(np.arange(len(df)))
        ts, sym, row = (np.concatenate(x) for x in (ts_all, sym_all, row_all))
        order = np.lexsort((sym, ts))                      # by time, then symbol
        self._ev_sym, self._ev_row = sym[order].tolist(), row[order].tolist()
        self.days = pd.DatetimeIndex(
            sorted(set().union(*(df.index.normalize().unique() for df in self.data.values())))
        )

    # ------------------------------------------------------------------
    def run(self, strategy: Strategy) -> BacktestResult:
        sig = {s: strategy.signals(self.data[sym]) for s, sym in enumerate(self.symbols)}
        side = [sig[s]["side"].tolist() for s in sig]
        stop = [sig[s]["stop"].tolist() for s in sig]
        target = [sig[s]["target"].tolist() for s in sig]
        exit_long = [sig[s]["exit_long"].tolist() for s in sig]
        exit_short = [sig[s]["exit_short"].tolist() for s in sig]

        O, H, L, C, DAY, MIN = self._o, self._h, self._l, self._c, self._day, self._minute
        sess = self.session
        first_min = sess.first_entry.hour * 60 + sess.first_entry.minute
        last_min = sess.last_entry.hour * 60 + sess.last_entry.minute
        sq_min = sess.square_off.hour * 60 + sess.square_off.minute
        slip = self.costs.slippage_bps / 10_000

        risk = RiskManager(self.risk_cfg)
        positions: dict[int, list] = {}     # sym -> [side, qty, entry_row, entry_px, stop, target, last_row]
        trades: list[tuple] = []
        skipped = {"risk_gate": 0, "bad_levels": 0, "too_small": 0, "shorts_off": 0}
        cur_day = None

        def close(s: int, row: int, price: float, reason: str) -> None:
            p_side, qty, e_row, e_px, p_stop, p_target, _ = positions.pop(s)
            buy_px, sell_px = (e_px, price) if p_side == 1 else (price, e_px)
            gross = (sell_px - buy_px) * qty
            charges = round_trip_charges(buy_px * qty, sell_px * qty, self.costs)
            net = gross - charges
            risk.record_exit(net)
            trades.append((s, p_side, qty, e_row, e_px, row, price, p_stop, p_target, reason, gross, charges, net))

        for s, i in zip(self._ev_sym, self._ev_row):
            d = DAY[s][i]
            if d != cur_day:
                # Anything still open means that day's data ended early: close at the last seen price.
                for ps in list(positions):
                    last = positions[ps][6]
                    close(ps, last, C[ps][last], "data_end")
                risk.start_day()
                cur_day = d
            m, o = MIN[s][i], O[s][i]
            prev_today = i > 0 and DAY[s][i - 1] == d
            pos = positions.get(s)
            closed_now = False

            # 1. Exits that happen at this candle's open.
            if pos is not None:
                p_side = pos[0]
                if m >= sq_min:
                    close(s, i, o * (1 - slip * p_side), "square_off")
                    pos, closed_now = None, True
                elif prev_today and (exit_long[s][i - 1] if p_side == 1 else exit_short[s][i - 1]):
                    close(s, i, o * (1 - slip * p_side), "signal_exit")
                    pos, closed_now = None, True

            # 2. Entry at this candle's open, from the previous candle's signal.
            if pos is None and not closed_now and prev_today and first_min <= m <= last_min:
                want = side[s][i - 1]
                if want != 0:
                    if want == -1 and not self.allow_short:
                        skipped["shorts_off"] += 1
                    elif not risk.can_enter(len(positions)):
                        skipped["risk_gate"] += 1
                    else:
                        px = o * (1 + slip * want)
                        st, tg = stop[s][i - 1], target[s][i - 1]
                        levels_ok = (st < px < tg) if want == 1 else (tg < px < st)
                        if not levels_ok:                 # gapped past the stop or target (or NaN)
                            skipped["bad_levels"] += 1
                        else:
                            qty = risk.position_size(px, st)
                            if qty < 1:
                                skipped["too_small"] += 1
                            else:
                                pos = [want, qty, i, px, st, tg, i]
                                positions[s] = pos
                                risk.record_entry()

            # 3. Stop and target inside this candle. Stop is checked first.
            if pos is not None:
                p_side, st, tg = pos[0], pos[4], pos[5]
                h, l = H[s][i], L[s][i]
                if p_side == 1:
                    if o <= st:
                        close(s, i, o * (1 - slip), "stop")
                    elif l <= st:
                        close(s, i, st * (1 - slip), "stop")
                    elif h >= tg:
                        close(s, i, tg, "target")
                    else:
                        pos[6] = i
                else:
                    if o >= st:
                        close(s, i, o * (1 + slip), "stop")
                    elif h >= st:
                        close(s, i, st * (1 + slip), "stop")
                    elif l <= tg:
                        close(s, i, tg, "target")
                    else:
                        pos[6] = i

        for ps in list(positions):
            last = positions[ps][6]
            close(ps, last, C[ps][last], "data_end")

        return self._result(strategy, trades, skipped)

    # ------------------------------------------------------------------
    def _result(self, strategy: Strategy, raw: list[tuple], skipped: dict) -> BacktestResult:
        rows = []
        for s, p_side, qty, e_row, e_px, x_row, x_px, st, tg, reason, gross, charges, net in raw:
            idx = self.data[self.symbols[s]].index
            rows.append((self.symbols[s], p_side, qty, idx[e_row], e_px, idx[x_row], x_px,
                         st, tg, reason, gross, charges, net))
        trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
        daily = pd.Series(0.0, index=self.days, name="net")
        if len(trades):
            trades = trades.sort_values(["entry_time", "symbol"]).reset_index(drop=True)
            by_day = trades.groupby(trades["exit_time"].dt.normalize())["net"].sum()
            daily = daily.add(by_day, fill_value=0.0)
        return BacktestResult(strategy.name, dict(strategy.params), trades, daily,
                              self.risk_cfg.capital, skipped)
