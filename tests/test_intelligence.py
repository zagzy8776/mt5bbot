"""Intelligence layer: context -> agents -> thesis signal, fail-closed behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mt5_platform.agents import Stance
from mt5_platform.runtime import IntelligenceLayer
from mt5_platform.runtime.intelligence import IntelligenceStats

T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)


def _feed_history(layer: IntelligenceLayer, n: int = 300) -> None:
    """Feed enough ticks to make the context usable (needs 30+ primary bars)."""
    price = 2500.0
    for i in range(n):
        # spread ticks across time so M1 candles form (60s spacing)
        ts = T0 + timedelta(seconds=60 * i)
        drift = ((i % 20) - 10) * 0.2
        price = 2500.0 + drift
        layer.feed_tick(bid=price - 0.15, ask=price + 0.15, timestamp=ts)


async def test_feed_tick_builds_context_fail_closed_on_insufficient_history() -> None:
    layer = IntelligenceLayer(symbol="XAUUSD")
    ctx = layer.feed_tick(bid=2500.0, ask=2500.3)
    assert ctx is not None
    assert ctx.usable_for_trading is False  # 1 tick is not a market
    assert layer.stats.contexts_built == 1 and layer.stats.errors == 0


async def test_thesis_never_directional_with_insufficient_context() -> None:
    """Fail-closed: garbage context must never become a BUY/SELL signal."""
    layer = IntelligenceLayer(symbol="XAUUSD")
    ctx = layer.feed_tick(bid=2500.0, ask=2500.3)
    thesis = layer.produce_thesis(ctx)
    assert thesis is not None
    assert thesis.action is Stance.NO_TRADE
    assert layer.thesis_to_signal(thesis) is None
    assert layer.stats.signals_from_thesis == 0


async def test_thesis_to_signal_carries_thesis_metadata() -> None:
    from mt5_platform.common.enums import OrderSide

    layer = IntelligenceLayer(symbol="XAUUSD")
    from mt5_platform.agents import TradeThesis

    thesis = TradeThesis(
        context_id="ctx_test",
        instrument="XAUUSD",
        action=Stance.BUY,
        direction=OrderSide.BUY,
        regime="trending",
        confidence=0.85,
        aligned_agents=5,
        opposed_agents=1,
        entry=2500.3,
        stop_loss=2495.0,
        take_profit=2510.0,
        reasons=["weighted buy conviction"],
        invalidation=["price closes beyond the stop-loss level"],
    )
    signal = layer.thesis_to_signal(thesis)
    assert signal is not None
    assert signal.direction is OrderSide.BUY
    assert signal.strategy_name == "intelligence_synthesis"
    assert signal.metadata["thesis_id"] == thesis.thesis_id
    assert signal.metadata["aligned_agents"] == 5
    assert signal.stop_loss == 2495.0
    assert layer.stats.signals_from_thesis == 1 and layer.stats.errors == 0


async def test_thesis_to_signal_refuses_no_trade_and_missing_levels() -> None:
    from mt5_platform.agents import TradeThesis
    from mt5_platform.common.enums import OrderSide

    layer = IntelligenceLayer(symbol="XAUUSD")
    no_trade = TradeThesis(
        context_id="ctx_test", instrument="XAUUSD", action=Stance.NO_TRADE
    )
    assert layer.thesis_to_signal(no_trade) is None
    missing_sl = TradeThesis(
        context_id="ctx_test",
        instrument="XAUUSD",
        action=Stance.BUY,
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=None,  # no stop -> refuse
    )
    assert layer.thesis_to_signal(missing_sl) is None
    assert layer.stats.signals_from_thesis == 0


async def test_evaluate_positions_handles_broker_positions() -> None:
    from mt5_platform.common.enums import OrderSide
    from mt5_platform.common.events import PositionInfo

    layer = IntelligenceLayer(symbol="XAUUSD")
    _feed_history(layer)
    ctx = layer.feed_tick(bid=2500.0, ask=2500.3)
    assert ctx is not None
    pos = PositionInfo(
        ticket="1001",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry_price=2500.0,
        current_price=2500.15,
        floating_pnl=0.15,
        stop_loss=2495.0,
        opened_at=T0,
    )
    results = layer.evaluate_positions([pos], ctx)
    assert len(results) == 1
    assert layer.stats.position_evaluations == 1 and layer.stats.errors == 0


async def test_record_completed_trade_stores_memory() -> None:
    from mt5_platform.common.enums import OrderSide
    from mt5_platform.common.events import StrategySignal

    layer = IntelligenceLayer(symbol="XAUUSD")
    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2495.0,
        strategy_name="intelligence_synthesis",
    )
    layer.record_completed_trade(
        signal=signal, entry=2500.0, exit_price=2502.0, volume=0.01, pnl=2.0
    )
    assert layer.stats.learning_records == 1 and layer.stats.errors == 0
    assert len(layer.decision_memory.all()) == 1


async def test_stats_never_negative_and_all_fields_present() -> None:
    stats = IntelligenceStats()
    assert stats.to_dict() == {
        "contexts_built": 0,
        "theses_emitted": 0,
        "theses_no_trade": 0,
        "signals_from_thesis": 0,
        "position_evaluations": 0,
        "position_exits": 0,
        "learning_records": 0,
        "errors": 0,
    }