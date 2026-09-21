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
from mt5_platform.orders import OrderManager
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
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.signal_engine = signal_engine
        self.risk_engine = risk_engine
        self.order_manager = order_manager
        self.symbol = (symbol or settings.default_symbol).strip().upper()
        self.timeframe = timeframe.strip().upper()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: TradingLoop | None = None
        self._snapshot = RuntimeSnapshot(symbol=self.symbol, timeframe=self.timeframe)

    @property
    def snapshot(self) -> dict[str, Any]:
        snap = self._snapshot
        if self._loop is not None:
            snap.connected = bool(getattr(self.adapter, "_connected", False))
            snap.kill_switch = self.risk_engine.kill_switch
            snap.stats = self._loop.stats.to_dict()
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
                )
                await self._loop.start()
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
                with asyncio.CancelledError:
                    pass
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
            with asyncio.CancelledError:
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
                self._task = None
                self._stop_event = None
                self._loop = None
                self._snapshot.state = "stopped"
                self._snapshot.connected = False
        return self.snapshot

    async def restart(self) -> dict[str, Any]:
        await self.stop()
        return await self.start()

    async def refresh_account(self) -> Any:
        if not getattr(self.adapter, "_connected", False):
            raise RuntimeError("MT5 runtime is not connected")
        return await self.adapter.get_account()

    async def refresh_positions(self) -> list[Any]:
        if not getattr(self.adapter, "_connected", False):
            raise RuntimeError("MT5 runtime is not connected")
        return await self.adapter.get_positions()


__all__ = ["BotControlService", "RuntimeSnapshot"]
