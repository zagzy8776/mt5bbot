"""Signal diagnostics: every "0 signals" must be explainable, never a bare counter.

Covers the engine snapshot (evaluations, last signal, exact rejection) and the loop's pipeline
stages: A no candle, B candle without setup, C/D signal vs risk rejection, E/F/G order outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mt5_platform.account import AccountMonitor
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import BotControlService, Quote, TradingLoop
from mt5_platform.signals import SignalEngine
from mt5_platform.strategy.base import Strategy
from tests.fake_mt5 import FakeMT5
from tests.test_runtime import AlwaysBuy, FakeFeed, _bars
from tests.test_symbol_case import _BrokerLikeAdapter

T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)


class NoStopStrategy(Strategy):
    """Emits a signal without a stop loss: the engine must reject it, with a reason."""

    name = "no_stop"

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal:
        return StrategySignal(
            symbol=event.symbol,
            direction=OrderSide.BUY,
            entry=2500.0,
            stop_loss=None,
            confidence=0.9,
            strategy_name=self.name,
            timestamp=event.timestamp,
        )

    def calculate_entry(self, event, direction):
        return 2500.0

    def calculate_stop_loss(self, entry, direction):
        return None

    def calculate_take_profit(self, entry, direction):
        return None

    def confidence(self, event) -> float:
        return 0.9


def _event(symbol: str = "XAUUSD") -> MarketDataEvent:
    return MarketDataEvent(timestamp=T0, source="test", symbol=symbol, bid=2500.0, ask=2500.3)


async def _rig(
    *,
    strategies: list[Strategy] | None = None,
    quote: Quote | None = None,
    fake_setup: object | None = None,
) -> tuple[TradingLoop, FakeMT5, FakeFeed, SignalEngine]:
    fake = FakeMT5(balance=10_000.0)
    if callable(fake_setup):
        fake_setup(fake)
    settings = Settings(execution_backend="mt5", risk_state_path="")
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    feed = FakeFeed(_bars(), quote or Quote(2500.0, 2500.3, 30.0, 0.0))
    engine = SignalEngine(list(strategies or []))
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=feed,
        signal_engine=engine,
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=["XAUUSD"],
        poll_s=0.01,
        warmup_bars=3,
        reconcile_every_s=0.0,
    )
    return loop, fake, feed, engine


# ------------------------------------------------------------------- engine snapshot


async def test_engine_snapshot_reports_evaluations_and_last_signal() -> None:
    engine = SignalEngine([AlwaysBuy()])
    emitted = await engine.on_market_data(_event())
    assert len(emitted) == 1

    snap = engine.stats_snapshot()
    assert snap["evaluations"] == 1
    assert snap["events_processed"] == 1
    assert snap["signals_generated"] == 1
    assert snap["signals_rejected"] == 0
    assert snap["last_evaluation_time"] == T0.isoformat()
    assert snap["last_evaluation_symbol"] == "XAUUSD"
    assert snap["last_signal_strategy"] == "always_buy"
    assert snap["last_signal_side"] == "buy"
    assert snap["last_signal_reason"] == ""  # AlwaysBuy does not set a reason
    assert snap["last_signal_time"] == T0.isoformat()
    assert snap["last_signal_stop_loss"] == 2495.15  # mid price (2500.15) - 5
    assert snap["last_rejection"] is None

    per = snap["strategy_stats"]["always_buy"]
    assert per["evaluations"] == 1 and per["signals"] == 1 and per["rejections"] == 0


async def test_engine_snapshot_records_the_exact_rejection_reason() -> None:
    engine = SignalEngine([NoStopStrategy()], require_stop_loss=True)
    emitted = await engine.on_market_data(_event())
    assert emitted == []

    snap = engine.stats_snapshot()
    assert snap["signals_generated"] == 0
    assert snap["signals_rejected"] == 1
    assert snap["reject_reasons"] == {"missing_stop_loss": 1}
    rejection = snap["last_rejection"]
    assert rejection is not None
    assert rejection["strategy"] == "no_stop"
    assert rejection["reasons"] == ["missing_stop_loss"]
    assert snap["strategy_stats"]["no_stop"]["rejections"] == 1


async def test_engine_snapshot_is_empty_before_the_first_candle() -> None:
    snap = SignalEngine([AlwaysBuy()]).stats_snapshot()
    assert snap["evaluations"] == 0
    assert snap["last_evaluation_time"] is None
    assert snap["last_signal_time"] is None
    assert snap["last_signal_strategy"] is None
    assert snap["last_rejection"] is None


# ------------------------------------------------------------------ pipeline stages


async def test_stage_b_survives_cycles_that_wait_for_the_next_candle() -> None:
    """A processed candle must stay visible instead of flipping back to a bare 'waiting'."""
    loop, _, feed, engine = await _rig(strategies=[AlwaysBuy()])
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    assert loop.last_cycle["stage"] == "order_filled"
    assert not loop.last_cycle.get("waiting")

    await loop.run_once()  # same candle: nothing new to evaluate
    trace = loop.last_cycle
    assert trace["stage"] == "order_filled"  # the real outcome is preserved
    assert trace["waiting"] is True
    assert trace["last_processed_bar"] == feed.bars[-1].time.isoformat()
    assert engine.stats_snapshot()["evaluations"] == 1  # live only; no new evaluation


class _NoBarsFeed:
    """A feed that returns nothing: the exact signature of the XAUUSDm casing outage."""

    async def quote(self, symbol: str) -> Quote:
        return Quote(2500.0, 2500.3, 30.0, 0.0)

    async def latest_closed_bar(self, symbol: str):
        return None

    async def history(self, symbol: str, count: int) -> list:
        return []


async def test_stage_a_waiting_for_a_new_closed_candle() -> None:
    loop, _, _, _ = await _rig()
    await loop.start()  # warm-up consumes the last bar; no new bar has closed yet
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "warmup_complete"  # primed, nothing live yet
    assert trace["waiting"] is True
    assert trace["last_processed_bar"] == _bars()[-1].time.isoformat()
    assert loop.stats.bars_processed == 0


async def test_stage_a_no_candle_data_is_loud_not_silent() -> None:
    """'no_candle_available' is the outage signature: the bot must say so, not sit at zero."""
    settings = Settings(execution_backend="mt5", risk_state_path="")
    adapter = _BrokerLikeAdapter()
    risk = RiskEngine(settings=settings)
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,  # type: ignore[arg-type]
        feed=_NoBarsFeed(),  # type: ignore[arg-type]
        signal_engine=SignalEngine([]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=["XAUUSDm"],
    )
    await loop.start()
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "no_candle_available"
    assert trace["symbol"] == "XAUUSDm"
    assert loop.stats.bars_processed == 0


async def test_stage_b_candle_evaluated_without_a_setup() -> None:
    loop, _, feed, engine = await _rig(strategies=[])
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "candle_evaluated_no_setup"
    assert trace["bar_time"] == feed.bars[-1].time.isoformat()
    assert loop.stats.bars_processed == 1
    # Live counters count live candles; the 3 warm-up bars live in the replay counters.
    assert engine.stats_snapshot()["evaluations"] == 1
    assert engine.stats_snapshot()["replay_evaluations"] == 3
    assert engine.stats_snapshot()["signals_generated"] == 0


async def test_stage_g_signal_became_a_filled_protected_order() -> None:
    loop, fake, feed, engine = await _rig(strategies=[AlwaysBuy()])
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "order_filled"
    assert trace["status"] == "filled"
    assert trace["order_id"] and trace["strategy"] == "always_buy" and trace["side"] == "buy"
    assert trace["stop_loss"] and trace["take_profit"]  # protection travelled with the order
    assert len(fake.sent) == 1
    assert engine.stats_snapshot()["last_signal_strategy"] == "always_buy"


async def test_stage_d_signal_rejected_by_risk_keeps_the_reason() -> None:
    loop, fake, feed, _ = await _rig(
        strategies=[AlwaysBuy()], quote=Quote(2500.0, 2503.0, 600.0, 0.0)
    )
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "signal_rejected_by_risk"
    assert trace["reasons"] == ["spread_too_wide"]
    assert trace["signal"]["strategy"] == "always_buy"
    assert fake.sent == []
    assert loop.stats.skipped["risk:spread_too_wide"] == 1


async def test_stage_f_broker_rejection_is_recorded_not_hidden() -> None:
    def reject(fake: FakeMT5) -> None:
        fake.next_send_retcode = FakeMT5.TRADE_RETCODE_REJECT

    loop, fake, feed, _ = await _rig(strategies=[AlwaysBuy()], fake_setup=reject)
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    trace = loop.last_cycle
    assert trace["stage"] == "order_rejected"
    assert trace["status"] == "broker_rejected"
    assert trace["rejection_reason"]  # the broker's reason is preserved
    assert len(fake.sent) == 1


async def test_warmup_replay_is_recorded_and_never_traded() -> None:
    """Historical signals prime the strategies but must never reach risk or the broker."""
    loop, fake, _, engine = await _rig(strategies=[AlwaysBuy()])
    await loop.start()

    replay = loop.warmup_replay
    assert replay["bars"] == 3  # the whole warm-up history
    assert replay["signals"] == 3  # AlwaysBuy fires on every bar
    assert fake.sent == []  # the past is never traded
    assert engine.stats_snapshot()["replay_signals_generated"] == 3
    assert engine.stats_snapshot()["signals_generated"] == 0  # nothing live yet

    trace = loop.last_cycle
    assert trace["stage"] == "warmup_complete"
    assert trace["bars_replayed"] == 3 and trace["signals_discarded"] == 3


async def test_service_stats_expose_the_engine_and_the_pipeline() -> None:
    loop, _, _, engine = await _rig(strategies=[AlwaysBuy()])
    await engine.on_market_data(_event())
    settings = Settings(execution_backend="mt5", risk_state_path="")
    service = BotControlService(
        settings=settings,
        adapter=loop.adapter,
        signal_engine=engine,
        risk_engine=loop.risk_engine,
        order_manager=loop.order_manager,
    )

    stopped = service.snapshot
    assert stopped["stats"]["pipeline_stage"] == "not_running"
    assert stopped["stats"]["signal_engine"]["signals_generated"] == 1

    service._loop = loop  # the service owns one loop; simulate the running state
    loop._stage("order_filled", order_id="o_1")
    running = service.snapshot
    assert running["stats"]["pipeline_stage"] == "order_filled"
    assert running["stats"]["pipeline"]["order_id"] == "o_1"
    assert running["stats"]["signal_engine"]["last_signal_strategy"] == "always_buy"
