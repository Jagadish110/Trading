"""Groww, through its official Trade API (the `growwapi` library).

The rest of the bot was written against Zerodha's client. Rather than
write the order logic twice, `GrowwAsKite` wraps a Groww client and answers
the same calls, so the broker, the price feed and the history download all
work unchanged.

The call names and arguments below were checked against growwapi 1.5.0.
The shape of Groww's replies (field names such as `groww_order_id`,
`order_status`, `average_fill_price`, `candles`) is from memory of Groww's
documentation and has NOT been checked against the real service. Run
paper or assist mode first: it uses the same price calls with no money at
risk, and any wrong field name shows up there.
"""
from __future__ import annotations

import base64
import hmac
import struct
import time
from datetime import datetime, timedelta

import pandas as pd

from .kite import IST

SEGMENT = "CASH"
MAX_SPAN_DAYS = {1: 7, 5: 15, 10: 30, 15: 30, 30: 90, 60: 150}     # longest span per candle request
INTERVAL_MIN = {"minute": 1, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}
ORDER_TYPES = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "SL", "SL-M": "SL_M"}
STATUS = {
    "EXECUTED": "COMPLETE", "COMPLETED": "COMPLETE", "DELIVERY_AWAITED": "COMPLETE",
    "REJECTED": "REJECTED", "FAILED": "REJECTED",
    "CANCELLED": "CANCELLED",
    "TRIGGER_PENDING": "TRIGGER PENDING",
}                                        # anything else (NEW, ACKED, APPROVED, OPEN ...) counts as still open


def totp(secret: str, at: float | None = None) -> str:
    """The 6-digit code an authenticator app would show for `secret`."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = struct.pack(">Q", int((time.time() if at is None else at) // 30))
    digest = hmac.new(key, counter, "sha1").digest()
    start = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[start:start + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def connect(env: dict):
    """A logged-in Groww client wrapped to look like Zerodha's.

    Needs GROWW_API_KEY and one of:
      GROWW_TOTP_SECRET   the bot logs in by itself every day, or
      GROWW_API_SECRET    you approve the key on Groww's site each morning.
    """
    try:
        from growwapi import GrowwAPI
    except ImportError as e:
        raise RuntimeError("The Groww library is not installed. Run: pip install growwapi") from e
    key = env.get("GROWW_API_KEY", "")
    if not key:
        raise RuntimeError("GROWW_API_KEY is not set. Create an API key at groww.in/trade-api and add it to .env.")
    try:
        if env.get("GROWW_TOTP_SECRET"):
            token = GrowwAPI.get_access_token(api_key=key, totp=totp(env["GROWW_TOTP_SECRET"]))
        elif env.get("GROWW_API_SECRET"):
            token = GrowwAPI.get_access_token(api_key=key, secret=env["GROWW_API_SECRET"])
        else:
            raise RuntimeError("Set GROWW_TOTP_SECRET or GROWW_API_SECRET in .env.")
        client = GrowwAsKite(GrowwAPI(token))
        client.profile()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Groww login failed ({type(e).__name__}: {e}). Check the keys in .env, and if "
                           "you use an API secret, approve the key on Groww's site today.") from e
    return client


class GrowwAsKite:
    """Answers the Zerodha client's calls using a Groww client."""

    def __init__(self, groww, sleep=time.sleep, pause: float = 0.25):
        self.g = groww
        self._sleep, self._pause = sleep, pause

    # -- market data -----------------------------------------------------
    def instruments(self, exchange: str = "NSE") -> list[dict]:
        df = self.g.get_all_instruments()
        df = df[(df["exchange"] == exchange) & (df["segment"] == SEGMENT)]
        if "series" in df.columns:
            df = df[df["series"].fillna("EQ") == "EQ"]
        return [{"tradingsymbol": r["trading_symbol"], "instrument_token": r["trading_symbol"],
                 "instrument_type": "EQ", "tick_size": float(r.get("tick_size") or 0.05)}
                for r in df.to_dict("records")]

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        minutes = INTERVAL_MIN[interval]
        step = timedelta(days=MAX_SPAN_DAYS[minutes])
        start, end = pd.Timestamp(from_date).to_pydatetime(), pd.Timestamp(to_date).to_pydatetime()
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        rows, seen, cursor = [], set(), start
        while cursor < end:
            upto = min(cursor + step, end)
            reply = self.g.get_historical_candle_data(
                trading_symbol=instrument_token, exchange="NSE", segment=SEGMENT,
                start_time=f"{cursor:%Y-%m-%d %H:%M:%S}", end_time=f"{upto:%Y-%m-%d %H:%M:%S}",
                interval_in_minutes=minutes)
            for c in (reply or {}).get("candles") or []:
                stamp = float(c[0])
                ts = pd.Timestamp(stamp, unit="ms" if stamp > 1e11 else "s", tz="UTC").tz_convert(IST)
                if ts not in seen:
                    seen.add(ts)
                    rows.append({"date": ts.to_pydatetime(), "open": c[1], "high": c[2], "low": c[3],
                                 "close": c[4], "volume": c[5] or 0})
            cursor = upto + timedelta(seconds=1)
            if cursor < end:
                self._sleep(self._pause)
        return sorted(rows, key=lambda r: r["date"])

    def ltp(self, *instruments):
        keys = {i.replace(":", "_", 1): i for i in instruments}       # NSE:SBIN -> NSE_SBIN
        out = {}
        names = list(keys)
        for k in range(0, len(names), 50):                             # Groww takes 50 symbols a call
            reply = self.g.get_ltp(exchange_trading_symbols=tuple(names[k:k + 50]), segment=SEGMENT) or {}
            for name, value in reply.items():
                if name in keys:
                    price = value.get("ltp") if isinstance(value, dict) else value
                    out[keys[name]] = {"last_price": float(price)}
        return out

    def profile(self):
        return self.g.get_user_profile()

    # -- orders ------------------------------------------------------------
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product,
                    order_type, price=None, validity=None, trigger_price=None, tag=None,
                    market_protection=None, **_):
        reply = self.g.place_order(
            validity=validity or "DAY", exchange=exchange, order_type=ORDER_TYPES[order_type],
            product=product, quantity=int(quantity), segment=SEGMENT, trading_symbol=tradingsymbol,
            transaction_type=transaction_type, price=float(price or 0.0), trigger_price=trigger_price)
        order_id = (reply or {}).get("groww_order_id")
        if not order_id:
            raise RuntimeError(f"Groww did not return an order id: {reply}")
        return str(order_id)

    def order_history(self, order_id):
        d = self.g.get_order_detail(segment=SEGMENT, groww_order_id=order_id) or {}
        raw = str(d.get("order_status") or d.get("status") or "").upper()
        avg = d.get("average_fill_price") or d.get("avg_fill_price") or d.get("average_price") or 0.0
        return [{"order_id": order_id, "status": STATUS.get(raw, "OPEN"),
                 "filled_quantity": int(d.get("filled_quantity") or 0), "average_price": float(avg),
                 "status_message": d.get("remark") or d.get("message")}]

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.g.cancel_order(groww_order_id=order_id, segment=SEGMENT)
        return order_id

    def positions(self):
        reply = self.g.get_positions_for_user(segment=SEGMENT) or {}
        net = []
        for p in reply.get("positions") or []:
            qty = p.get("quantity")
            if qty is None:
                qty = (p.get("credit_quantity") or 0) - (p.get("debit_quantity") or 0)
            net.append({"tradingsymbol": p.get("trading_symbol"), "quantity": int(qty),
                        "product": p.get("product"), "exchange": p.get("exchange")})
        return {"net": net, "day": []}
