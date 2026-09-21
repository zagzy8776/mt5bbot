"""The trading loop: MT5 candles -> strategies -> money-correct sizing -> risk -> order -> broker.

Fail-closed by design: any unexpected error pauses that cycle, repeated errors engage the
kill switch (persisted), and broker-side SL/TP keep protecting open positions if this
process stops. The loop never closes positions on its own initiative.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.data import bar_to_event
from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderSide, Severity
from mt5_platform.common.events import AuditEvent, StrategySignal
from mt5_platform.common.instruments import position_size_for_risk
from mt5_platform.config import Settings
from mt5_platform.execution.base import ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.runtime.feed import MarketFeed
from mt5_platform.signals import SignalEngine


@dataclass
class LoopStats:
    cycles: int = 0
    bars_processed: int = 0
    signals: int = 0
    orders_sent: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    skipped: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {**self.__dict__, "skipped": dict(self.skipped)}


class TradingLoop:
    def __init__(
        self,
        *,
        settings: Settings,
        adapter: ExecutionAdapter,
        feed: MarketFeed,
        signal_engine: SignalEngine,
        risk_engine: RiskEngine,
        order_manager: OrderManager,
        monitor: AccountMonitor,
        symbols: list[str],
        risk_pct: float | None = None,
        poll_s: float = 5.0,
        warmup_bars: int = 200,
        max_consecutive_errors: int = 5,
        reconcile_every_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        intelligence: Any | None = None,
    ) -> None:
        self.settings, self.adapter, self.feed = settings, adapter, feed
        self.signal_engine, self.risk_engine = signal_engine, risk_engine
        self.order_manager, self.monitor = order_manager, monitor
        self.symbols = [s.upper() for s in symbols]
        self.risk_pct = risk_pct if risk_pct is not None else settings.max_risk_per_trade_pct
        self.poll_s, self.warmup_bars = poll_s, warmup_bars
        self.max_consecutive_errors = max_consecutive_errors
        self.reconcile_every_s = reconcile_every_s
        self._clock = clock
        self._last_bar: dict[str, object] = {}
        self._last_reconcile = float("-inf")
        self.stats = LoopStats()
        self.intelligence = intelligence

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect, recover state from the broker, and warm strategies on closed history."""
        self.signal_engine.reset_state()
        await self.adapter.connect()
        await self.order_manager.reconcile(self.adapter)
        for symbol in self.symbols:
            bars = await self.feed.history(symbol, self.warmup_bars)
            spec = await self.adapter.get_instrument(symbol)
            for bar in bars:  # signals from history are discarded: never trade the past
                await self.signal_engine.on_market_data(
                    bar_to_event(bar, symbol, bar.spread * (spec.tick_size if spec else 0.0))
                )
            if bars:
                self._last_bar[symbol] = bars[-1].time

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        await self.run_started(stop)

    async def run_started(self, stop: asyncio.Event) -> None:
        """Run the polling cycle after start() has completed successfully."""
        while not stop.is_set():
            try:
                await self.run_once()
                self.stats.consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive and fail closed
                self.stats.errors += 1
                self.stats.consecutive_errors += 1
                self._audit(
                    Severity.ERROR,
                    {"stage": "cycle", "consecutive": self.stats.consecutive_errors},
                    str(exc),
                )
                if self.stats.consecutive_errors >= self.max_consecutive_errors:
                    self.risk_engine.engage_kill_switch("trading_loop_errors")
                    self._audit(Severity.CRITICAL, {"stage": "loop_halted"}, str(exc))
                    return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
            except TimeoutError:
                pass
    async def run_once(self) -> None:
        self.stats.cycles += 1
        if not await self.adapter.is_connected():
            await self.adapter.connect()
        account = await self.adapter.get_account()
        self.monitor.evaluate(account)  # engages the (persisted) kill switch on breach
        if self._clock() - self._last_reconcile >= self.reconcile_every_s:
            await self.order_manager.reconcile(self.adapter)
            self._last_reconcile = self._clock()

        for symbol in self.symbols:
            quote = await self.feed.quote(
                symbol
            )  # polled every cycle: keeps staleness tracking live
            bar = await self.feed.latest_closed_bar(symbol)
            if bar is None or bar.time == self._last_bar.get(symbol):
                continue
            self._last_bar[symbol] = bar.time
            self.stats.bars_processed += 1
            spec = await self.adapter.get_instrument(symbol)
            spread_price = bar.spread * spec.tick_size if spec and bar.spread > 0 else 0.0
            signals = await self.signal_engine.on_market_data(
                bar_to_event(bar, symbol, spread_price)
            )
            for signal in signals:
                self.stats.signals += 1
                account = await self.adapter.get_account()
                await self._handle_signal(signal, account, quote)

        # Intelligence layer (optional, behind INTELLIGENCE_ENABLED flag)
        if self.intelligence is not None and quote is not None:
            await self._run_intelligence(quote)

    async def _run_intelligence(self, quote: Any) -> None:
        """Feed tick to intelligence layer, produce thesis signals, evaluate positions."""
        try:
            ctx = self.intelligence.feed_tick(bid=quote.bid, ask=quote.ask)
            if ctx is None or not ctx.usable_for_trading:
                return
            thesis = self.intelligence.produce_thesis(ctx)
            if thesis is not None:
                signal = self.intelligence.thesis_to_signal(thesis)
                if signal is not None:
                    self.stats.signals += 1
                    account = await self.adapter.get_account()
                    await self._handle_signal(signal, account, quote)
            # Evaluate open positions via PositionManager
            positions = await self.adapter.get_positions()
            if positions:
                results = self.intelligence.evaluate_positions(positions, ctx)
                from mt5_platform.common.enums import PositionDecision
                for pos, decision in results:
                    if decision.decision in (
                        PositionDecision.EXIT,
                        PositionDecision.EMERGENCY_EXIT,
                    ):
                        try:
                            await self.adapter.close_position(pos.ticket)
                            self.intelligence.stats.position_exits += 1
                        except Exception:
                            self.intelligence.stats.errors += 1
        except Exception:
            if self.intelligence is not None:
                self.intelligence.stats.errors += 1

    async def _handle_signal(self, signal: StrategySignal, account, quote) -> None:
        if quote is None:
            self.stats.skipped["no_quote"] += 1
            return
        spec = await self.adapter.get_instrument(signal.symbol)
        if spec is None:
            self.stats.skipped["no_instrument_spec"] += 1
            return
        if signal.stop_loss is None:
            self.stats.skipped["no_stop_loss"] += 1
            return
        positions = await self.adapter.get_positions()
        est_entry = quote.ask if signal.direction is OrderSide.BUY else quote.bid
        volume = position_size_for_risk(
            equity=account.equity,
            risk_pct=self.risk_pct,
            entry=est_entry,
            stop=signal.stop_loss,
            spec=spec,
            max_volume=self.settings.max_position_size,
        )
        if volume <= 0:
            # The correct trade on a small account is often no trade. Never round up.
            self.stats.skipped["size_too_small"] += 1
            self._audit(
                Severity.INFO,
                {
                    "reason": "size_too_small",
                    "symbol": signal.symbol,
                    "equity": account.equity,
                    "risk_pct": self.risk_pct,
                },
            )
            return
        ctx = RiskContext(
            account=account,
            open_positions=len(positions),
            current_spread=quote.spread_points,
            data_age_ms=quote.age_ms,
            duplicate_position=any(
                p.symbol == signal.symbol and p.side == signal.direction for p in positions
            ),
            market_session_ok=quote.age_ms < 45_000,
            proposed_volume=volume,
            instrument=spec,
            execution_entry=est_entry,
        )
        _, decision, record = await self.order_manager.process_signal(signal, ctx, self.adapter)
        if record is not None:
            self.stats.orders_sent += 1
        elif not decision.approved:
            for reason in decision.reasons:
                self.stats.skipped[f"risk:{reason}"] += 1

    def _audit(self, severity: Severity, payload: dict, error: str | None = None) -> None:
        audit_log.emit(
            AuditEvent(
                component="trading_loop",
                event_type=AuditEventType.EXECUTION_ERROR.value,
                severity=severity,
                payload=payload,
                error=error,
            )
        )
