"""Warm-up/replay isolation: history may prime the strategies, never the live records.

A warm-up replay evaluates historical candles so the strategies have state. Those signals:

* must never be traded (no risk check, no order),
* must never appear as live operational signals (no store sinks, no live feed, no live counters),
* must carry an explicit replay tag wherever they are kept,
* must be counted separately from live activity so the dashboard can tell them apart.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.account import AccountMonitor
from mt5_platform.common.events import StrategySignal
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import BotControlService, Quote, TradingLoop
from mt5_platform.signals import AuditStoreSink, SignalEngine, SignalStoreSink
from tests.fake_mt5 import FakeMT5
from tests.test_runtime import AlwaysBuy, FakeFeed, _bars


class RecordingStore:
    """Minimal MarketDataStore stand-in: records exactly what the real sinks persist."""

    def __init__(self) -> None:
        self.signals: list[StrategySignal] = []
        self.audits: list[Any] = []

    async def write_signal(self, signal: StrategySignal) -> None:
        self.signals.append(signal)

    async def write_audit(self, event: Any) -> None:
        self.audits.append(event)


async def _rig(
    *,
    strategies: list[Any] | None = None,
) -> tuple[TradingLoop, FakeMT5, FakeFeed, SignalEngine, RecordingStore]:
    fake = FakeMT5(balance=10_000.0)
    settings = Settings(execution_backend="mt5", risk_state_path="")
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    store = RecordingStore()
    engine = SignalEngine(
        list(strategies or []),
        sink=SignalStoreSink(store),
        audit_sink=AuditStoreSink(store),
    )
    feed = FakeFeed(_bars(), Quote(2500.0, 2500.3, 30.0, 0.0))
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
    return loop, fake, feed, engine, store


# ------------------------------------------------- 1. replay never reaches the broker


async def test_warmup_signals_never_reach_order_submission() -> None:
    loop, fake, _, engine, _ = await _rig(strategies=[AlwaysBuy()])
    await loop.start()  # AlwaysBuy fires on every warm-up bar

    assert engine.replay_signals_generated == 3
    assert loop.warmup_replay["signals"] == 3
    assert fake.sent == []  # no broker request of any kind
    assert loop.stats.orders_sent == 0 and loop.stats.signals == 0
    assert loop.last_cycle["stage"] == "warmup_complete"
    assert loop.last_cycle["signals_discarded"] == 3


# ------------------------------------- 2. replay never looks like a live signal record


async def test_warmup_signals_are_absent_from_operational_storage_and_feed() -> None:
    loop, _, _, engine, store = await _rig(strategies=[AlwaysBuy()])
    await loop.start()

    assert store.signals == []  # nothing written to the operational signal store
    assert store.audits == []  # and no SIGNAL_GENERATED audit rows for the replay
    assert engine.recent_signals() == []  # the live feed (/api/v1/signals) stays empty
    assert engine.stats.signals_generated == 0
    assert engine.last_signal is None  # replay never becomes "the last live signal"


async def test_warmup_signals_are_explicitly_tagged_as_replay() -> None:
    loop, _, _, engine, _ = await _rig(strategies=[AlwaysBuy()])
    await loop.start()

    replay = engine.replay_signals()
    assert len(replay) == 3
    assert all(s.metadata.get("replay") is True for s in replay)
    assert all(s.metadata.get("session") == "warmup" for s in replay)


# --------------------------------------------- 3. live signals are persisted normally


async def test_live_signals_are_still_persisted_and_untagged() -> None:
    loop, fake, feed, engine, store = await _rig(strategies=[AlwaysBuy()])
    await loop.start()
    assert store.signals == []  # only the replay happened so far

    feed.add_bar()
    await loop.run_once()  # one live candle -> one live signal

    assert len(store.signals) == 1  # persisted exactly like before
    live = store.signals[0]
    assert live.metadata.get("replay") is None and live.metadata.get("session") is None
    assert any(event.event_type == "SIGNAL_GENERATED" for event in store.audits)
    assert len(engine.recent_signals()) == 1
    assert engine.replay_signals_generated == 3  # unchanged by live activity
    assert len(fake.sent) == 1  # and it became an order as usual


# ------------------------------------------ 4./5. counters and the dashboard separate


async def test_runtime_counters_and_stats_distinguish_replay_from_live() -> None:
    loop, _, feed, engine, _ = await _rig(strategies=[AlwaysBuy()])
    await loop.start()
    feed.add_bar()
    await loop.run_once()

    snap = engine.stats_snapshot()
    assert snap["evaluations"] == 1  # live candles only
    assert snap["replay_evaluations"] == 3  # warm-up candles
    assert snap["signals_generated"] == 1  # live signals only
    assert snap["replay_signals_generated"] == 3  # replay signals
    assert snap["replay_persisted"] is False
    per = snap["strategy_stats"]["always_buy"]
    assert (per["evaluations"], per["signals"]) == (1, 1)
    assert (per["replay_evaluations"], per["replay_signals"]) == (3, 3)

    settings = Settings(execution_backend="mt5", risk_state_path="")
    service = BotControlService(
        settings=settings,
        adapter=loop.adapter,
        signal_engine=engine,
        risk_engine=loop.risk_engine,
        order_manager=loop.order_manager,
    )
    service._loop = loop  # the service owns exactly one loop
    stats = service.snapshot["stats"]
    assert stats["bars_processed"] == 1  # live candles processed
    assert stats["signal_engine"]["evaluations"] == 1
    assert stats["signal_engine"]["signals_generated"] == 1
    assert stats["signal_engine"]["replay_evaluations"] == 3
    assert stats["signal_engine"]["replay_signals_generated"] == 3
    assert stats["warmup_replay"]["bars"] == 3
    assert stats["warmup_replay"]["signals"] == 3


async def test_a_new_warmup_session_resets_only_the_replay_counters() -> None:
    loop, _, _, engine, _ = await _rig(strategies=[AlwaysBuy()])
    await loop.start()
    assert engine.replay_signals_generated == 3

    await loop.start()  # restart: a fresh warm-up session
    assert engine.replay_signals_generated == 3  # current session only, not accumulated
    assert engine.replay_evaluations == 3
    assert len(engine.replay_signals()) == 3
