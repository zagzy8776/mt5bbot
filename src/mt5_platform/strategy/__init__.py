"""Strategy package — pluggable signal generators (no order placement)."""

from __future__ import annotations

from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.breakout import BreakoutStrategy
from mt5_platform.strategy.mean_reversion import MeanReversionStrategy
from mt5_platform.strategy.momentum import MomentumStrategy
from mt5_platform.strategy.null_strategy import NullStrategy
from mt5_platform.strategy.registry import (
    available_strategies,
    create_strategies,
    create_strategy,
    describe_available,
)
from mt5_platform.strategy.sma_crossover import SmaCrossoverStrategy

__all__ = [
    "BreakoutStrategy",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "NullStrategy",
    "SmaCrossoverStrategy",
    "Strategy",
    "available_strategies",
    "create_strategies",
    "create_strategy",
    "describe_available",
    "mid_price",
]
