"""Strategy plug-ins. Add a new one by subclassing Strategy and listing it here."""
from .base import Strategy
from .ema_trend import EmaTrend
from .orb import OpeningRangeBreakout
from .rsi_reversion import RsiReversion
from .supertrend import SupertrendFlip
from .vwap_pullback import VwapPullback

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (OpeningRangeBreakout, VwapPullback, EmaTrend, RsiReversion, SupertrendFlip)
}

__all__ = ["Strategy", "REGISTRY"]
