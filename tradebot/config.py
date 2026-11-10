"""All tunable settings in one place.

Nothing here is a recommendation. The defaults are conservative starting
points; change them to match your own capital and comfort.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class RiskConfig:
    capital: float = 100_000.0          # rupees the bot sizes positions from
    risk_per_trade_pct: float = 0.5     # % of capital lost if a stop is hit
    max_daily_loss_pct: float = 2.0     # no new entries after losing this much in a day
    max_trades_per_day: int = 6
    max_open_positions: int = 3
    max_position_pct: float = 50.0      # cap on one position's value, % of capital
    min_stop_pct: float = 0.1           # skip trades whose stop is closer than this % of price
    compound: bool = False              # False = size every day from starting capital


@dataclass(frozen=True)
class SessionConfig:
    first_entry: time = time(9, 20)     # earliest bar a new position may open on
    last_entry: time = time(14, 45)     # no new positions after this
    square_off: time = time(15, 15)     # everything still open is closed here


@dataclass(frozen=True)
class CostConfig:
    """Zerodha equity intraday (MIS) charges on NSE.

    Checked against zerodha.com/charges on 2026-11-10. These change from
    time to time, so re-check before trusting a backtest.
    """
    brokerage_pct: float = 0.03         # per executed order ...
    brokerage_cap: float = 20.0         # ... or this many rupees, whichever is lower
    stt_sell_pct: float = 0.025         # sell side only
    txn_pct: float = 0.00307            # NSE transaction charge, both sides
    sebi_per_crore: float = 10.0        # both sides
    gst_pct: float = 18.0               # on brokerage + SEBI + transaction charge
    stamp_buy_pct: float = 0.003        # buy side only
    slippage_bps: float = 5.0           # 5 bps = 0.05% worse fill on entries, stops, square-off


# Liquid large caps to start with. Index membership changes, so review this list.
DEFAULT_WATCHLIST = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "ITC",
    "BHARTIARTL", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA",
    "TITAN", "NTPC", "M&M", "TATASTEEL", "HCLTECH",
]
