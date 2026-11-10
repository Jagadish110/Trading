"""Brokerage, taxes and exchange charges for one round trip."""
from __future__ import annotations

from .config import CostConfig


def round_trip_charges(buy_value: float, sell_value: float, cfg: CostConfig) -> float:
    """Total charges in rupees for buying `buy_value` and selling `sell_value`.

    Works for shorts too: the buy is simply the later (covering) leg.
    """
    brokerage = (
        min(buy_value * cfg.brokerage_pct / 100, cfg.brokerage_cap)
        + min(sell_value * cfg.brokerage_pct / 100, cfg.brokerage_cap)
    )
    turnover = buy_value + sell_value
    stt = sell_value * cfg.stt_sell_pct / 100
    txn = turnover * cfg.txn_pct / 100
    sebi = turnover * cfg.sebi_per_crore / 1e7
    gst = (brokerage + sebi + txn) * cfg.gst_pct / 100
    stamp = buy_value * cfg.stamp_buy_pct / 100
    return brokerage + stt + txn + sebi + gst + stamp
