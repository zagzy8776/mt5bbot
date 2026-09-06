"""Strategy registry / factory."""

from __future__ import annotations

from mt5_platform.strategy.base import Strategy
from mt5_platform.strategy.breakout import BreakoutStrategy
from mt5_platform.strategy.mean_reversion import MeanReversionStrategy
from mt5_platform.strategy.momentum import MomentumStrategy
from mt5_platform.strategy.null_strategy import NullStrategy
from mt5_platform.strategy.sma_crossover import SmaCrossoverStrategy

STRATEGY_FACTORIES = {
    "null": lambda **_: NullStrategy(),
    "sma_crossover": lambda **kwargs: SmaCrossoverStrategy(**kwargs),
    "breakout": lambda **kwargs: BreakoutStrategy(**kwargs),
    "mean_reversion": lambda **kwargs: MeanReversionStrategy(**kwargs),
    "momentum": lambda **kwargs: MomentumStrategy(**kwargs),
}


def available_strategies() -> list[str]:
    return sorted(STRATEGY_FACTORIES.keys())


def describe_available() -> list[dict]:
    """Factory catalog with default parameters — for API / dashboard docs."""
    return [create_strategy(name).info() for name in available_strategies()]


def create_strategy(name: str, **kwargs) -> Strategy:
    key = name.strip().lower()
    factory = STRATEGY_FACTORIES.get(key)
    if factory is None:
        raise ValueError(f"unknown strategy: {name}. available={available_strategies()}")
    return factory(**kwargs)


def create_strategies(names: list[str], **shared_kwargs) -> list[Strategy]:
    return [create_strategy(name, **shared_kwargs) for name in names]
