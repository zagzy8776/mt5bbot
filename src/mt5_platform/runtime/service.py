"""Service wrapper for running and controlling the live MT5 trading loop.

The trading runtime is deliberately separate from the HTTP layer. The service owns one
TradingLoop task at a time, exposes a serializable status snapshot, and shuts down cleanly
without touching open broker positions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.validation import live_block_reason, read_validation
from mt5_platform.config import Settings
from mt5_platform.execution.base import ExecutionAdapter
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.historical.outcome_loader import evidence_status, load_live_outcomes
from mt5_platform.orders import OrderManager
from mt5_platform.outcomes import OutcomeLearningPipeline, TradeOutcomeRecorder
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime.feed import MT5CandleFeed
from mt5_platform.runtime.loop import TradingLoop
from mt5_platform.signals import SignalEngine


@dataclass
class RuntimeSnapshot:
    state: str = "stopped"
    symbol: str = "XAUUSD"
    timeframe: str = "M15"
    started_at: str | None = None
    last_error: str | None = None
    connected: bool = False
    kill_switch: bool = False
    stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "connected": self.connected,
            "kill_switch": self.kill_switch,
            "stats": self.stats or {},
        }


class BotControlService:
    """Own exactly one live TradingLoop task and provide safe lifecycle controls."""

    def __init__(
        self,
        *,
        settings: Settings,
        adapter: ExecutionAdapter,
        signal_engine: SignalEngine,
        risk_engine: RiskEngine,
        order_manager: OrderManager,
        symbol: str | None = None,
        timeframe: str = "M15",
        store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.signal_engine = signal_engine
        self.risk_engine = risk_engine
        self.order_manager = order_manager
        self.symbol = (symbol or settings.default_symbol).strip()
        self.timeframe = timeframe.strip().upper()
        self.store = store
        # Outcome ledger: recording is passive and can never block or alter an order.
        self.learning = OutcomeLearningPipeline()
        self.ledger = InMemoryHistoricalLedger()
        self.outcome_recorder = (
            TradeOutcomeRecorder(store, on_completed=self._on_outcome_completed)
            if store is not None
            else None
        )
        self._ledger_loaded = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: TradingLoop | None = None
        self._snapshot = RuntimeSnapshot(symbol=self.symbol, timeframe=self.timeframe)

    @property
    def snapshot(self) -> dict[str, Any]:
        snap = self._snapshot
        stats: dict[str, Any] = dict(self._loop.stats.to_dict()) if self._loop is not None else {}
        # Diagnostics are exposed even while stopped, so "0 signals" is always explainable.
        stats["signal_engine"] = self.signal_engine.stats_snapshot()
        stats["pipeline"] = self._loop.last_cycle if self._loop is not None else {}
        stats["pipeline_stage"] = (
            self._loop.last_cycle.get("stage") if self._loop is not None else "not_running"
        )
        stats["warmup_replay"] = self._loop.warmup_replay if self._loop is not None else {}
        stats["position_management"] = (
            self._loop.position_management if self._loop is not None else {}
        )
        if self._loop is not None:
            snap.connected = bool(getattr(self.adapter, "_connected", False))
            snap.kill_switch = self.risk_engine.kill_switch
            if self._loop.intelligence is not None:
                stats["intelligence"] = self._loop.intelligence.stats.to_dict()
        # Outcome ledger + learning status: always visible, whether or not the loop is running.
        stats["outcomes"] = (
            self.outcome_recorder.summary() if self.outcome_recorder is not None else {}
        )
        stats["learning"] = self.learning.stats()
        stats["evidence"] = self.evidence_status()
        # Can this runtime actually transmit an order right now? (AutoTrading off => 10027.)
        availability = getattr(self.adapter, "execution_availability", None)
        stats["execution"] = availability() if callable(availability) else {}
        snap.stats = stats
        return snap.to_dict()

    def _validate_start(self) -> None:
        if self.settings.execution_backend.strip().lower() != "mt5":
            raise ValueError(
                "runtime control requires EXECUTION_BACKEND=mt5; the mock backend is test-only"
            )
        if not isinstance(self.adapter, MT5ExecutionAdapter):
            raise ValueError("runtime requires an MT5 execution adapter")

        if self.settings.is_live:
            report = read_validation(self.settings.validation_report_path)
            reason = live_block_reason(
                report,
                symbol=self.symbol,
                timeframe=self.timeframe,
            )
            if reason:
                raise ValueError(f"LIVE TRADING BLOCKED: {reason}")

    def configure(self, *, symbol: str | None = None, timeframe: str | None = None) -> None:
        """Change runtime target while stopped."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("stop the runtime before changing symbol/timeframe")
        if symbol:
            self.symbol = symbol.strip()
        if timeframe:
            self.timeframe = timeframe.strip().upper()
        self._snapshot.symbol = self.symbol
        self._snapshot.timeframe = self.timeframe

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return self.snapshot

            self._validate_start()
            self._snapshot.state = "starting"
            self._snapshot.last_error = None
            self._snapshot.symbol = self.symbol
            self._snapshot.timeframe = self.timeframe

            try:
                self._stop_event = asyncio.Event()
                intelligence = None
                if getattr(self.settings, "intelligence_enabled", False):
                    from mt5_platform.runtime.intelligence import IntelligenceLayer
                    intelligence = IntelligenceLayer(
                        symbol=self.symbol, timeframe=self.timeframe
                    )
                self._loop = TradingLoop(
                    settings=self.settings,
                    adapter=self.adapter,
                    feed=MT5CandleFeed(self.adapter, self.timeframe),
                    signal_engine=self.signal_engine,
                    risk_engine=self.risk_engine,
                    order_manager=self.order_manager,
                    monitor=AccountMonitor(self.settings, self.risk_engine),
                    symbols=[self.symbol],
                    poll_s=5.0,
                    warmup_bars=200,
                    intelligence=intelligence,
                    outcome_recorder=self.outcome_recorder,
                )
                await self._loop.start()
                await self._load_evidence_ledger()
                self._snapshot.started_at = datetime.now(UTC).isoformat()
                self._snapshot.state = "running"
                self._snapshot.connected = True
                self._snapshot.kill_switch = self.risk_engine.kill_switch
                self._task = asyncio.create_task(
                    self._run_started_loop(self._loop, self._stop_event),
                    name="mt5-trading-loop",
                )
                return self.snapshot
            except Exception as exc:
                self._snapshot.state = "error"
                self._snapshot.last_error = str(exc)
                self._snapshot.connected = False
                self._loop = None
                self._task = None
                self._stop_event = None
                try:
                    await self.adapter.disconnect()
                except Exception:
                    pass
                raise

    async def _run_started_loop(self, loop: TradingLoop, stop: asyncio.Event) -> None:
        try:
            await loop.run_started(stop)
            self._snapshot.state = (
                "halted" if self.risk_engine.kill_switch else "stopped"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._snapshot.state = "error"
            self._snapshot.last_error = str(exc)
        finally:
            self._snapshot.connected = False
            try:
                await self.adapter.disconnect()
            except Exception as exc:
                self._snapshot.last_error = str(exc)

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            task = self._task
            stop = self._stop_event
            if task is None or task.done():
                try:
                    await self.adapter.disconnect()
                except Exception:
                    pass
                self._snapshot.state = "stopped"
                self._snapshot.connected = False
                return self.snapshot

            self._snapshot.state = "stopping"
            if stop is not None:
                stop.set()

        try:
            await asyncio.wait_for(task, timeout=15.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            try:
                await self.adapter.disconnect()
            except Exception as exc:
                self._snapshot.last_error = str(exc)
            async with self._lock:
                if self._loop is not None:
                    self._snapshot.stats = self._loop.stats.to_dict()
                    self._snapshot.kill_switch = self.risk_engine.kill_switch
                self._task = None
                self._stop_event = None
                self._loop = None
                self._snapshot.state = "stopped"
                self._snapshot.connected = False
        return self.snapshot

    async def restart(self) -> dict[str, Any]:
        await self.stop()
        return await self.start()

    # ---------------------------------------------------------------- outcome ledger

    def _on_outcome_completed(self, outcome: HistoricalOutcome) -> None:
        """A trade finished: review it and add it to the evidence ledger.

        This is the only place a completed trade enters the learning layer. It cannot change any
        trading configuration: reviews are recorded and lessons are proposed; nothing is applied.
        """
        try:
            intelligence = self._loop.intelligence if self._loop is not None else None
            self.learning.record(outcome, intelligence=intelligence)
            self.ledger.record(outcome)
        except Exception as exc:  # learning must never affect the trading path
            self._snapshot.last_error = f"outcome learning failed: {exc}"

    async def _load_evidence_ledger(self) -> int:
        """database -> historical outcomes -> ledger -> EvidenceEngine.

        Called once per start: without this the evidence engine is permanently empty and every
        decision sees "insufficient evidence" no matter how much the bot has traded.
        """
        if self.store is None or self._ledger_loaded:
            return 0
        try:
            outcomes = await load_live_outcomes(self.store)
        except Exception as exc:
            self._snapshot.last_error = f"evidence ledger load failed: {exc}"
            return 0
        self.ledger.record_many(outcomes)
        self._ledger_loaded = True
        if self._loop is not None and self._loop.intelligence is not None:
            self._loop.intelligence.historical_ledger.record_many(outcomes)
        return len(outcomes)

    def evidence_status(self) -> dict[str, Any]:
        """What the EvidenceEngine can actually see right now (never a lowered threshold)."""
        return evidence_status(self.ledger, instrument=self.symbol)

    async def load_outcomes(
        self, *, limit: int = 100, status: str | None = None
    ) -> list[HistoricalOutcome]:
        """Persisted outcomes for the dashboard (store first, in-memory recorder as fallback)."""
        if self.store is not None:
            try:
                return await self.store.get_outcomes(limit=limit, status=status)
            except Exception as exc:
                self._snapshot.last_error = f"outcome query failed: {exc}"
        if self.outcome_recorder is None:
            return []
        rows = self.outcome_recorder.all_outcomes()
        if status:
            rows = [row for row in rows if row.status.value == status]
        return sorted(rows, key=lambda row: row.timestamp, reverse=True)[:limit]

    async def outcomes_payload(
        self, *, limit: int = 100, status: str | None = None
    ) -> dict[str, Any]:
        """Dashboard payload: separate populations, data availability and evidence quality."""
        await self._load_evidence_ledger()  # the dashboard must see persisted evidence too
        records = await self.load_outcomes(limit=limit, status=status)
        summary = (
            self.outcome_recorder.summary() if self.outcome_recorder is not None else {}
        )
        return {
            "count": len(records),
            "summary": summary,
            "learning": self.learning.stats(),
            "evidence": self.evidence_status(),
            "outcomes": [row.model_dump(mode="json") for row in records],
        }

    async def refresh_account(self) -> Any:
        if not getattr(self.adapter, "_connected", False):
            raise RuntimeError("MT5 runtime is not connected")
        return await self.adapter.get_account()

    async def refresh_positions(self) -> list[Any]:
        if not getattr(self.adapter, "_connected", False):
            raise RuntimeError("MT5 runtime is not connected")
        return await self.adapter.get_positions()


__all__ = ["BotControlService", "RuntimeSnapshot"]
