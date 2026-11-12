"""Settings for swing trading. Nothing here is a recommendation."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskConfig


def swing_risk(capital: float = 20_000.0) -> RiskConfig:
    return RiskConfig(
        capital=capital,
        risk_per_trade_pct=1.0,         # % of capital lost if a stop is hit
        max_open_positions=3,
        max_position_pct=30.0,          # cap on one position's value
        min_stop_pct=1.0,               # skip trades whose stop is closer than this
        max_trades_per_day=3,
        max_daily_loss_pct=100.0,       # not used: stops and position limits do this job
    )


@dataclass(frozen=True)
class DeliveryCosts:
    """Zerodha equity delivery charges on NSE.

    From memory of zerodha.com/charges; could not be re-checked while
    building this. Check them before trusting a backtest.
    """
    brokerage: float = 0.0              # delivery is free at Zerodha
    stt_pct: float = 0.1                # both buy and sell
    txn_pct: float = 0.00307            # both sides
    sebi_per_crore: float = 10.0
    gst_pct: float = 18.0               # on brokerage + SEBI + transaction charge
    stamp_buy_pct: float = 0.015
    dp_charge: float = 15.34            # flat, each time a stock is sold from the demat account
    slippage_bps: float = 10.0          # 0.1% worse fill at the open and on stops


def delivery_charges(buy_value: float, sell_value: float, c: DeliveryCosts) -> float:
    turnover = buy_value + sell_value
    txn = turnover * c.txn_pct / 100
    sebi = turnover * c.sebi_per_crore / 1e7
    gst = (2 * c.brokerage + sebi + txn) * c.gst_pct / 100
    return (2 * c.brokerage + turnover * c.stt_pct / 100 + txn + sebi + gst
            + buy_value * c.stamp_buy_pct / 100 + c.dp_charge)


MAX_HOLD_DAYS = 15                       # sell anything held this many trading days

# Nifty 50 members, from memory. Index membership changes, so review this
# list. Symbols that cannot be downloaded are skipped with a note.
SWING_UNIVERSE = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
    "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]
