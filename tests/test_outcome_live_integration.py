"""Outcome recorder wired into the real trading loop (FakeMT5 broker, no live orders).

Proves the runtime lifecycle end to end:

    loop places the order -> recorder observes the position at the broker -> MAE/MFE tracked ->
    broker closes the position (stop-out) -> reconciliation finalizes with real money from the
    closing deal -> the outcome is persisted -> review recorded and evidence ledger fed.

The recorder is passive: a broken store must not stop the trading loop (last tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.data import Bar
from mt5_platform.common.enums import ExitCause, OrderSide, OutcomeSource, OutcomeStatus
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.outcome_loader import evidence_status
from mt5_platform.orders import OrderManager
from mt5_platform.outcomes import OutcomeLearningPipeline, TradeOutcomeRecorder
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import Quote, TradingLoop
from mt5_platform.signals import SignalEngine
from mt5_platform.storage import InMemoryMarketDataStore
from mt5_platform.strategy.base import Strategy, mid_price
from tests.fake_mt5 import FakeMT5

T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)
SYMBOL = "XAUUSD"


class AlwaysBuy(Strategy):
    name = "always_buy"

    def generate_signal(self, event: MarketDataEvent):
        entry = mid_price(event)
        return StrategySignal(
            symbol=event.symbol,
            direction=OrderSide.BUY,
            entry=entry,
            stop_loss=entry - 5,
            take_profit=entry + 10,
            confidence=0.9,
            strategy_name=self.name,
            timestamp=event.timestamp,
        )

    def calculate_entry(self, event, direction):
        return mid_price(event)

    def calculate_stop_loss(self, entry, direction):
        return None

    def calculate_take_profit(self, entry, direction):
        return None

    def confidence(self, event):
        return 0.9


class FakeFeed:
    timeframe = "M15"

    def __init__(self, bars: list[Bar], quote: Quote) -> None:
        self.bars, self._quote = bars, quote

    async def latest_closed_bar(self, symbol):
        return self.bars[-1] if self.bars else None

    async def history(self, symbol, count):
        return self.bars[-count:]

    async def quote(self, symbol):
        return self._quote

    def add_bar(self) -> None:
        """A newly closed candle (the loop never re-evaluates the bar it already processed)."""
        last = self.bars[-1]
        self.bars.append(
            Bar(last.time + timedelta(minutes=15), 2500, 2501, 2499, 2500, 10, 25)
        )


def _bars(n: int = 3) -> list[Bar]:
    return [Bar(T0 + timedelta(minutes=15 * i), 2500, 2501, 2499, 2500, 10, 25) for i in range(n)]


class _BrokenStore(InMemoryMarketDataStore):
    """A database that is down: recording must degrade, trading must continue."""

    async def write_outcome(self, outcome) -> None:
        raise RuntimeError("database unavailable")

    async def get_open_outcomes(self):
        raise RuntimeError("database unavailable")

    async def get_outcomes(self, **kwargs):
        raise RuntimeError("database unavailable")


def _loop(store, *, learning=None, symbol: str = SYMBOL):
    fake = FakeMT5(balance=10_000.0)
    settings = Settings(execution_backend="mt5", default_symbol=symbol)
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    feed = FakeFeed(_bars(), Quote(2500.0, 2500.3, 30.0, 0.0))
    recorder = TradeOutcomeRecorder(
        store, on_completed=(learning.record if learning is not None else None)
    )
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=feed,
        signal_engine=SignalEngine([AlwaysBuy()]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=[symbol],
        poll_s=0.01,
        warmup_bars=3,
        reconcile_every_s=0.0,  # reconcile every cycle so the test can observe the lifecycle
        outcome_recorder=recorder,
    )
    return loop, fake, adapter, recorder


async def test_autonomous_trade_produces_a_persisted_outcome_end_to_end() -> None:
    store = InMemoryMarketDataStore()
    learning = OutcomeLearningPipeline()
    loop, fake, adapter, recorder = _loop(store, learning=learning)

    await loop.start()
    assert await recorder.resume() == 0

    loop.feed.add_bar()  # a new closed candle is what the loop evaluates
    await loop.run_once()  # cycle 1: signal -> risk -> order filled at the broker
    assert loop.stats.orders_sent == 1
    assert len(fake.positions) == 1
    ticket = str(fake.positions[0].ticket)
    position = (await adapter.get_positions())[0]

    await loop.run_once()  # cycle 2: reconciliation discovers the position
    record = recorder.get(ticket)
    assert record is not None
    assert record.status is OutcomeStatus.OPEN
    assert record.source is OutcomeSource.AUTONOMOUS
    assert record.strategy == "always_buy"  # attributed through the entry context
    assert record.timeframe == "M15"
    assert record.instrument == SYMBOL
    assert record.entry == position.entry_price
    assert record.entry_volume == position.volume
    assert record.initial_stop_loss == position.stop_loss
    assert record.legs[0].kind == "entry"
    assert recorder.stats.opened == 1

    # The stop is hit at the broker while the bot runs (FakeMT5 records the closing deal).
    stop_level = float(fake.positions[0].sl)
    fake.order_send(
        {
            "action": 1,
            "symbol": SYMBOL,
            "position": int(ticket),
            "volume": float(fake.positions[0].volume),
            "price": stop_level,
            "type": FakeMT5.DEAL_TYPE_SELL,
            "comment": "sl",
        }
    )
    assert fake.positions == []

    await loop.run_once()  # cycle 3: no position left -> the outcome is finalized
    outcome = recorder.get(ticket)
    assert outcome is not None
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.exit_cause is ExitCause.STOP_LOSS
    assert outcome.exit_cause_source == "level_match"  # matched against the recorded stop
    assert outcome.exit_price == stop_level
    assert outcome.realized_pnl < 0  # from the broker's closing deal, not invented
    assert outcome.mae > 0 and outcome.excursion_samples > 0
    assert outcome.duration_s >= 0

    await recorder.flush()
    assert await store.count_outcomes(status="closed") == 1
    stored = (await store.get_outcomes(limit=5))[0]
    assert stored.trade_id == outcome.trade_id

    # learning received it, and the evidence ledger now sees exactly one live sample
    assert learning.stats()["reviews"] == 1
    assert learning.reviews[outcome.trade_id].actual_exit_reason == "stop_hit"
    ledger = InMemoryHistoricalLedger()
    ledger.record(outcome)
    status = evidence_status(ledger, instrument=SYMBOL)
    assert status["sample_size"] == 1
    assert status["evidence_quality"] == "insufficient"  # honest: 1 < 10


async def test_broken_outcome_store_never_stops_the_trading_loop() -> None:
    store = _BrokenStore()
    loop, fake, _adapter, recorder = _loop(store)

    await loop.start()  # resume() fails safely
    loop.feed.add_bar()
    await loop.run_once()  # cycle 1: the order reaches the broker
    await loop.run_once()  # cycle 2: the position is observed and the write fails

    assert loop.stats.orders_sent == 1  # trading continued
    assert fake.positions, "the order reached the broker"
    assert recorder.stats.persist_failures >= 1  # writes failed and stayed queued
    assert "persist failed" in (recorder.stats.last_error or "")
    assert recorder.open_outcomes(), "records are kept in memory for retry"


async def test_loop_without_a_recorder_behaves_exactly_as_before() -> None:
    loop, fake, _adapter, _recorder = _loop(None)
    await loop.start()
    loop.feed.add_bar()
    await loop.run_once()
    assert loop.stats.orders_sent == 1
    assert len(fake.positions) == 1
