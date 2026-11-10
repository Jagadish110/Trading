"""The gate every trade must pass.

The strategy proposes; this decides whether the trade is allowed and how
big it may be. The backtester uses it now, and paper and live trading will
use this same class, so the limits behave the same everywhere.
"""
from __future__ import annotations

import math

from .config import RiskConfig


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.total_pnl = 0.0        # realised, net of costs, since the start
        self.day_pnl = 0.0          # realised, net of costs, today
        self.trades_today = 0
        self.halted = False         # manual kill switch

    # -- lifecycle -------------------------------------------------------
    def start_day(self) -> None:
        self.day_pnl = 0.0
        self.trades_today = 0

    def record_entry(self) -> None:
        self.trades_today += 1

    def record_exit(self, net_pnl: float) -> None:
        self.day_pnl += net_pnl
        self.total_pnl += net_pnl

    # -- decisions -------------------------------------------------------
    @property
    def sizing_capital(self) -> float:
        if self.cfg.compound:
            return max(0.0, self.cfg.capital + self.total_pnl)
        return self.cfg.capital

    @property
    def daily_loss_limit(self) -> float:
        return self.sizing_capital * self.cfg.max_daily_loss_pct / 100

    def can_enter(self, open_positions: int) -> bool:
        if self.halted:
            return False
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False
        if open_positions >= self.cfg.max_open_positions:
            return False
        return self.day_pnl > -self.daily_loss_limit

    def position_size(self, entry: float, stop: float) -> int:
        """Shares to trade so that hitting the stop loses `risk_per_trade_pct`.
        Returns 0 when the trade should be skipped."""
        per_share = abs(entry - stop)
        if entry <= 0 or per_share <= 0:
            return 0
        if per_share / entry * 100 < self.cfg.min_stop_pct:
            return 0
        capital = self.sizing_capital
        by_risk = capital * self.cfg.risk_per_trade_pct / 100 / per_share
        by_value = capital * self.cfg.max_position_pct / 100 / entry
        return max(0, math.floor(min(by_risk, by_value)))
