"""A stand-in for growwapi.GrowwAPI with the same method and argument
names. The reply shapes are the ones the adapter assumes (see the note at
the top of tradebot/bot/groww.py)."""
from __future__ import annotations

from typing import Optional

import pandas as pd


class FakeGroww:
    def __init__(self, data: dict[str, pd.DataFrame] | None = None):
        self.data = data or {}
        self.price: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.placed: list[dict] = []
        self.candle_calls: list[tuple] = []
        self.reject: set[str] = set()
        self._n = 0

    def get_all_instruments(self):
        rows = [{"exchange": "NSE", "segment": "CASH", "series": "EQ", "trading_symbol": s,
                 "tick_size": 0.05} for s in (self.data or self.price)]
        rows.append({"exchange": "BSE", "segment": "CASH", "series": "EQ", "trading_symbol": "BSEONLY",
                     "tick_size": 0.05})
        return pd.DataFrame(rows)

    def get_historical_candle_data(self, trading_symbol: str, exchange: str, segment: str, start_time: str,
                                   end_time: str, interval_in_minutes: Optional[int] = None,
                                   timeout: Optional[int] = None):
        self.candle_calls.append((trading_symbol, start_time, end_time, interval_in_minutes))
        df = self.data[trading_symbol]
        df = df[(df.index >= pd.Timestamp(start_time)) & (df.index <= pd.Timestamp(end_time))]
        stamps = df.index.tz_localize("Asia/Kolkata").tz_convert("UTC")
        return {"candles": [[int(t.timestamp()), r.open, r.high, r.low, r.close, r.volume]
                            for t, r in zip(stamps, df.itertuples())]}

    def get_ltp(self, exchange_trading_symbols, segment: str, timeout: Optional[int] = None):
        assert segment == "CASH" and len(exchange_trading_symbols) <= 50
        return {k: self.price[k.split("_", 1)[1]] for k in exchange_trading_symbols}

    def get_user_profile(self, timeout: Optional[int] = None):
        return {"vendor_user_id": "abc"}

    def place_order(self, validity: str, exchange: str, order_type: str, product: str, quantity: int,
                    segment: str, trading_symbol: str, transaction_type: str,
                    order_reference_id: Optional[str] = None, price: Optional[float] = 0.0,
                    trigger_price: Optional[float] = None, timeout: Optional[int] = None):
        assert (validity, exchange, segment, product) == ("DAY", "NSE", "CASH", "MIS")
        assert order_type in ("LIMIT", "MARKET", "SL", "SL_M")
        self._n += 1
        oid = f"GMK{self._n:04d}"
        o = {"groww_order_id": oid, "trading_symbol": trading_symbol, "transaction_type": transaction_type,
             "quantity": quantity, "order_type": order_type, "price": price, "trigger_price": trigger_price,
             "order_status": "OPEN", "filled_quantity": 0, "average_fill_price": 0.0, "remark": None}
        self.orders[oid] = o
        self.placed.append(dict(o))
        last = self.price[trading_symbol]
        if order_type in self.reject:
            o.update(order_status="REJECTED", remark="Insufficient margin")
        elif order_type == "MARKET" or (order_type == "LIMIT" and (
                last <= price if transaction_type == "BUY" else last >= price)):
            o.update(order_status="EXECUTED", filled_quantity=quantity, average_fill_price=last)
        elif order_type == "SL_M":
            o["order_status"] = "TRIGGER_PENDING"
        return {"groww_order_id": oid, "order_status": o["order_status"]}

    def move(self, symbol, price):
        self.price[symbol] = price
        for o in self.orders.values():
            if o["order_status"] == "TRIGGER_PENDING" and o["trading_symbol"] == symbol:
                hit = price <= o["trigger_price"] if o["transaction_type"] == "SELL" else price >= o["trigger_price"]
                if hit:
                    o.update(order_status="EXECUTED", filled_quantity=o["quantity"], average_fill_price=price)

    def get_order_detail(self, segment: str, groww_order_id: str, timeout: Optional[int] = None):
        return dict(self.orders[groww_order_id])

    def cancel_order(self, groww_order_id: str, segment: str, timeout: Optional[int] = None):
        o = self.orders[groww_order_id]
        if o["order_status"] in ("EXECUTED", "REJECTED", "CANCELLED"):
            raise RuntimeError("Order cannot be cancelled")
        o["order_status"] = "CANCELLED"
        return {"groww_order_id": groww_order_id, "order_status": "CANCELLED"}

    def get_positions_for_user(self, segment: Optional[str] = None, timeout: Optional[int] = None):
        net: dict[str, int] = {}
        for o in self.orders.values():
            sign = 1 if o["transaction_type"] == "BUY" else -1
            net[o["trading_symbol"]] = net.get(o["trading_symbol"], 0) + sign * o["filled_quantity"]
        return {"positions": [{"trading_symbol": s, "quantity": q, "product": "MIS", "exchange": "NSE"}
                              for s, q in net.items()]}
