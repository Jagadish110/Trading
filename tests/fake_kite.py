"""A stand-in for kiteconnect.KiteConnect with the same method names and
argument names, so the real order and data code can be exercised offline.

Orders are judged against `self.price[symbol]`, which the test moves.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd


class FakeKite:
    def __init__(self, data: dict[str, pd.DataFrame] | None = None, now=None):
        self.data = data or {}
        self.now = now                                   # callable giving the simulated time
        self.price: dict[str, float] = {}
        self.orders_by_id: dict[str, dict] = {}
        self.placed: list[dict] = []                     # every place_order call, in order
        self.reject: set[str] = set()                    # order types to reject outright
        self.never_fill: set[str] = set()                # order types that just sit there
        self.fail_cancel = False
        self._n = 0

    # -- market data -----------------------------------------------------
    def instruments(self, exchange=None):
        return [{"tradingsymbol": s, "instrument_token": 1000 + i, "instrument_type": "EQ",
                 "tick_size": 0.05} for i, s in enumerate(self.data)]

    def _symbol(self, token):
        return list(self.data)[token - 1000]

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        df = self.data[self._symbol(instrument_token)]
        now = self.now()
        df = df[(df.index >= pd.Timestamp(from_date)) & (df.index <= min(pd.Timestamp(to_date), pd.Timestamp(now)))]
        return [{"date": ts.tz_localize("Asia/Kolkata").to_pydatetime(), "open": r.open, "high": r.high,
                 "low": r.low, "close": r.close, "volume": r.volume} for ts, r in df.iterrows()]

    def ltp(self, *instruments):
        out = {}
        for key in instruments:
            sym = key.split(":")[1]
            if self.data and self.now is not None:       # price inside the candle now forming
                df = self.data[sym]
                now = pd.Timestamp(self.now())
                bar = df[df.index <= now].iloc[-1]
                self.price[sym] = bar.open if now - bar.name < timedelta(minutes=1) else bar.close
            out[key] = {"instrument_token": 0, "last_price": self.price[sym]}
            self._work_stops(sym)
        return out

    def profile(self):
        return {"user_id": "AB1234"}

    # -- orders ----------------------------------------------------------
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product,
                    order_type, price=None, validity=None, validity_ttl=None, disclosed_quantity=None,
                    trigger_price=None, iceberg_legs=None, iceberg_quantity=None, auction_number=None,
                    algo_id=None, tag=None, market_protection=None):
        assert (variety, exchange, product) == ("regular", "NSE", "MIS")
        assert transaction_type in ("BUY", "SELL") and quantity > 0
        if order_type in ("MARKET", "SL-M"):
            assert market_protection is not None, "market orders need market protection"
        self._n += 1
        oid = str(self._n)
        o = {"order_id": oid, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
             "quantity": quantity, "order_type": order_type, "price": price, "trigger_price": trigger_price,
             "status": "OPEN", "filled_quantity": 0, "average_price": 0.0, "status_message": None}
        self.orders_by_id[oid] = o
        self.placed.append(dict(o))
        last = self.price[tradingsymbol]
        if order_type in self.reject:
            o.update(status="REJECTED", status_message="Insufficient funds")
        elif order_type in self.never_fill:
            pass
        elif order_type == "MARKET":
            o.update(status="COMPLETE", filled_quantity=quantity, average_price=last)
        elif order_type == "LIMIT":
            ok = last <= price if transaction_type == "BUY" else last >= price
            if ok:
                o.update(status="COMPLETE", filled_quantity=quantity, average_price=last)
        elif order_type == "SL-M":
            o["status"] = "TRIGGER PENDING"
        return oid

    def _work_stops(self, sym):
        last = self.price[sym]
        for o in self.orders_by_id.values():
            if o["status"] == "TRIGGER PENDING" and o["tradingsymbol"] == sym:
                hit = last <= o["trigger_price"] if o["transaction_type"] == "SELL" else last >= o["trigger_price"]
                if hit:
                    o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=last)

    def move(self, symbol, price):
        self.price[symbol] = price
        self._work_stops(symbol)

    def order_history(self, order_id):
        return [dict(self.orders_by_id[order_id])]

    def cancel_order(self, variety, order_id, parent_order_id=None):
        o = self.orders_by_id[order_id]
        if self.fail_cancel or o["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
            raise RuntimeError("Order cannot be cancelled")
        o["status"] = "CANCELLED"
        return order_id

    def positions(self):
        net: dict[str, int] = {}
        for o in self.orders_by_id.values():
            sign = 1 if o["transaction_type"] == "BUY" else -1
            net[o["tradingsymbol"]] = net.get(o["tradingsymbol"], 0) + sign * o["filled_quantity"]
        return {"net": [{"tradingsymbol": s, "quantity": q, "product": "MIS", "exchange": "NSE"}
                        for s, q in net.items()], "day": []}
