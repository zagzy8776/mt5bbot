"""The trading loop: MT5 candles -> strategies -> money-correct sizing -> risk -> order -> broker.

Fail-closed by design: any unexpected error pauses that cycle, repeated errors engage the
kill switch (persisted), and broker-side SL/TP keep protecting open positions if this
process stops.

Position lifecycle decisions (including exits) are produced by the PositionManager and always
pass RiskEngine -> OrderManager before reaching the broker, so the loop never closes anything
on its own initiative. Broker positions that were not opened by this bot follow
MANUAL_POSITION_POLICY (ignore | observe | manage).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.data import bar_to_event
from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import (
    AuditEventType,
    OrderSide,
    OrderStatus,
    PositionDecision,
    Severity,
)
from mt5_platform.common.events import (
    AuditEvent,
    ExecutionRecord,
    PositionInfo,
    RiskDecision,
    StrategySignal,
    utc_now,
)
from mt5_platform.common.instruments import position_size_for_risk
from mt5_platform.config import ManualPositionPolicy, Settings
from mt5_platform.execution.base import ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.outcomes.exit_cause import cause_from_position_decision
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
        return {**self.__dict__, "skipped": dict(self.skipped), "skip_reasons": dict(self.skipped)}


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
        outcome_recorder: Any | None = None,
        news_ingestor: Any | None = None,
    ) -> None:
        self.settings, self.adapter, self.feed = settings, adapter, feed
        self.signal_engine, self.risk_engine = signal_engine, risk_engine
        self.order_manager, self.monitor = order_manager, monitor
        # NEVER upper-case broker symbols: ids are case-sensitive (XAUUSDm != XAUUSDM). An
        # upper-cased symbol does not exist at the broker, so the feed returns no candles and
        # the bot silently evaluates nothing (live outage: 0 candles, 0 signals).
        self.symbols = [s.strip() for s in symbols]
        self.risk_pct = risk_pct if risk_pct is not None else settings.max_risk_per_trade_pct
        self.poll_s, self.warmup_bars = poll_s, warmup_bars
        self.max_consecutive_errors = max_consecutive_errors
        self.reconcile_every_s = reconcile_every_s
        self._clock = clock
        self._last_bar: dict[str, object] = {}
        self._last_reconcile = float("-inf")
        # Lifecycle tracking: tickets seen on the broker, and the last broker view of each.
        self._discovered_tickets: set[str] = set()
        self._position_fingerprints: dict[str, tuple[object, ...]] = {}
        # Most recent candle -> signal -> risk -> order trace (diagnostics, not control flow).
        self._last_cycle: dict[str, Any] = {}
        # Historical warm-up replay: bars evaluated and signals discarded (never traded).
        self._warmup_replay: dict[str, Any] = {}
        self.stats = LoopStats()
        self.intelligence = intelligence
        # Passive outcome recorder: observes broker truth and freezes HistoricalOutcome records.
        # It can never block, delay or alter an order (see outcomes/recorder.py).
        self.outcome_recorder = outcome_recorder
        self._symbol_tick: dict[str, float] = {}
        # Timeframe of the configured feed (outcome attribution only; the loop is tf-agnostic).
        self.timeframe = str(getattr(feed, "timeframe", "") or "")
        # Phase 2: recent closed candles per symbol, used for the candle-shape snapshot at entry.
        self._bar_history: dict[str, deque] = {}
        # Phase 3: in-trade management trace (what the position manager decided, and what happened).
        self._position_counts: Counter = Counter()
        self._position_management: dict[str, Any] = {}
        # Phase 4: macro calendar ingestion + blackout state.
        self.news_ingestor = news_ingestor
        self._last_news_poll = float("-inf")
        self._news_state: dict[str, Any] = {}
        # Entry context per (symbol, side): lets the recorder attribute a new position to the
        # signal that opened it (strategy, thesis, evidence, risk decision).
        self._entry_context: dict[tuple[str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect, recover state from the broker, and warm strategies on closed history."""
        self.signal_engine.reset_state()
        await self.adapter.connect()
        await self.order_manager.reconcile(self.adapter)
        replay: dict[str, Any] = {"bars": 0, "signals": 0, "symbols": {}}
        for symbol in self.symbols:
            bars = await self.feed.history(symbol, self.warmup_bars)
            spec = await self.adapter.get_instrument(symbol)
            if spec is not None:
                self._symbol_tick[symbol] = float(spec.tick_size)
            discarded = 0
            for bar in bars:  # replay history to prime the strategies: never traded, never stored
                emitted = await self.signal_engine.on_market_data(
                    bar_to_event(bar, symbol, bar.spread * (spec.tick_size if spec else 0.0)),
                    replay=True,
                )
                discarded += len(emitted)
            replay["bars"] += len(bars)
            replay["signals"] += discarded
            replay["symbols"][symbol] = {"bars": len(bars), "signals_discarded": discarded}
            if bars:
                self._last_bar[symbol] = bars[-1].time
                self._bar_history[symbol] = deque(bars[-60:], maxlen=60)
        # Keep the historical replay visible: it explains the engine counters before the first
        # live candle and proves those signals were never sent to risk or the broker.
        self._warmup_replay = replay
        last_seed = self._last_bar.get(self.symbols[0]) if self.symbols else None
        self._stage(
            "warmup_complete",
            bars_replayed=replay["bars"],
            signals_discarded=replay["signals"],
            last_replayed_bar=(
                last_seed.isoformat() if hasattr(last_seed, "isoformat") else None
            ),
        )
        await self._start_outcome_recorder()

    async def _start_outcome_recorder(self) -> None:
        """Attach the outcome ledger to broker truth: reload incomplete records, then reconcile.

        Never fatal: a recording problem must not stop the trading runtime.
        """
        recorder = self.outcome_recorder
        if recorder is None:
            return
        try:
            resumed = await recorder.resume()
            positions = await self.adapter.get_positions()
            await self._reconcile_outcomes(positions)
            await recorder.flush()
            self._stage(
                "outcomes_resumed",
                resumed=resumed,
                open_positions=len(positions),
                open_outcomes=len(recorder.open_outcomes()),
            )
        except Exception as exc:
            self.stats.errors += 1
            self._audit(
                Severity.WARNING,
                {"stage": "outcome_startup_failed", "error": str(exc)},
            )

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        await self.run_started(stop)

    async def run_started(self, stop: asyncio.Event) -> None:
        """Run the polling cycle after start() has completed successfully.

        The loop survives connection blips by attempting reconnection instead of
        halting. Only truly unrecoverable errors (max_consecutive_errors reached
        AND reconnect fails) engage the kill switch and exit.
        """
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
                    # Attempt reconnection before giving up
                    try:
                        self._audit(
                            Severity.WARNING,
                            {"stage": "reconnect_attempt"},
                            f"attempting reconnect after {self.stats.consecutive_errors} errors",
                        )
                        await self.adapter.disconnect()
                        await self.adapter.connect()
                        await self.order_manager.reconcile(self.adapter)
                        self.stats.consecutive_errors = 0
                        self._audit(
                            Severity.INFO,
                            {"stage": "reconnect_success"},
                            "reconnected after consecutive errors",
                        )
                    except Exception as reconnect_exc:  # noqa: BLE001
                        self.risk_engine.engage_kill_switch("trading_loop_errors")
                        self._audit(
                            Severity.CRITICAL,
                            {"stage": "loop_halted"},
                            f"reconnect failed: {reconnect_exc}",
                        )
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
            await self._reconcile_positions()
            self._last_reconcile = self._clock()
        if self.outcome_recorder is not None:
            await self._track_outcomes()
        if (
            self.news_ingestor is not None
            and self._clock() - self._last_news_poll >= float(self.settings.news_poll_interval_s)
        ):
            await self._poll_news()

        quote: Any | None = None
        new_bar_seen = False
        missing_data = False
        for symbol in self.symbols:
            quote = await self.feed.quote(
                symbol
            )  # polled every cycle: keeps staleness tracking live
            bar = await self.feed.latest_closed_bar(symbol)
            if bar is None:
                missing_data = True
                continue
            if bar.time == self._last_bar.get(symbol):
                continue
            self._last_bar[symbol] = bar.time
            self._bar_history.setdefault(symbol, deque(maxlen=60)).append(bar)
            self.stats.bars_processed += 1
            new_bar_seen = True
            spec = await self.adapter.get_instrument(symbol)
            spread_price = bar.spread * spec.tick_size if spec and bar.spread > 0 else 0.0
            signals = await self.signal_engine.on_market_data(
                bar_to_event(bar, symbol, spread_price)
            )
            if not signals:
                # Stage B: a closed candle was evaluated, no strategy produced a setup.
                self._stage(
                    "candle_evaluated_no_setup",
                    symbol=symbol,
                    bar_time=bar.time.isoformat(),
                    bar_open=bar.open,
                    bar_close=bar.close,
                    bar_high=bar.high,
                    bar_low=bar.low,
                )
            for signal in signals:
                self.stats.signals += 1
                account = await self.adapter.get_account()
                await self._handle_signal(signal, account, quote)

        if not new_bar_seen:
            symbol = self.symbols[0] if self.symbols else None
            last_processed = self._last_bar.get(symbol) if symbol else None
            wait_detail: dict[str, Any] = {
                # Stage A only until a candle has actually been evaluated; afterwards the last
                # real outcome (B..G) stays visible and "waiting" is reported as metadata.
                "symbol": symbol,
                "last_processed_bar": (
                    last_processed.isoformat() if hasattr(last_processed, "isoformat") else None
                ),
                "poll_s": self.poll_s,
                "waiting": True,
            }
            if missing_data or not self._last_cycle:
                self._stage(
                    "no_candle_available" if missing_data else "waiting_for_closed_candle",
                    **wait_detail,
                )
            else:
                self._last_cycle = {
                    **self._last_cycle,
                    "at": utc_now().isoformat(),
                    **wait_detail,
                }

        # Intelligence layer (optional, behind INTELLIGENCE_ENABLED flag)
        if self.intelligence is not None and quote is not None:
            await self._run_intelligence(quote)

    @property
    def last_cycle(self) -> dict[str, Any]:
        """Most recent pipeline trace (empty until the first cycle has run)."""
        return dict(self._last_cycle)

    @property
    def warmup_replay(self) -> dict[str, Any]:
        """Bars and signals from the historical warm-up (those signals are never traded)."""
        return dict(self._warmup_replay)

    def _stage(self, stage: str, **detail: Any) -> None:
        """Record where the candle -> signal -> risk -> order pipeline ended up.

        Stages: no_candle_available / waiting_for_closed_candle (no candle),
        candle_evaluated_no_setup (strategy condition false), signal_not_actionable,
        signal_rejected_by_risk, order_submitted, order_rejected, order_filled.
        """
        self._last_cycle = {
            "cycle": self.stats.cycles,
            "stage": stage,
            "at": utc_now().isoformat(),
            **detail,
        }

    def _stage_order(
        self, signal: StrategySignal, record: ExecutionRecord, decision: RiskDecision
    ) -> None:
        """Record the end of the chain: submitted, broker-rejected, or filled."""
        status = record.final_status
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            stage = "order_filled"
        elif status is OrderStatus.BROKER_REJECTED:
            stage = "order_rejected"
        else:
            stage = "order_submitted"
        self._stage(
            stage,
            symbol=signal.symbol,
            strategy=signal.strategy_name,
            side=signal.direction.value,
            order_id=record.order_id,
            status=status.value,
            risk_approved=decision.approved,
            volume=record.filled_volume or record.requested_volume,
            entry=record.execution_price or signal.entry,
            stop_loss=record.stop_loss or signal.stop_loss,
            take_profit=record.take_profit or signal.take_profit,
            rejection_reason=record.rejection_reason,
            signal_reason=signal.reason,
        )

    async def _reconcile_positions(self) -> list[PositionInfo]:
        """Record broker truth for every position: bot-owned and external/manual alike.

        POSITION_DISCOVERED is emitted once per ticket; POSITION_RECONCILED is emitted when a
        ticket first appears or when its broker view changes (side/volume/SL/TP). The trail
        therefore stays bounded and every entry means the broker state actually moved.
        """
        positions = await self.adapter.get_positions()
        for position in positions:
            fingerprint = (
                position.side.value,
                position.volume,
                position.stop_loss,
                position.take_profit,
            )
            if position.ticket not in self._discovered_tickets:
                self._discovered_tickets.add(position.ticket)
                self._position_event(AuditEventType.POSITION_DISCOVERED, Severity.INFO, position)
            if self._position_fingerprints.get(position.ticket) != fingerprint:
                self._position_fingerprints[position.ticket] = fingerprint
                self._position_event(AuditEventType.POSITION_RECONCILED, Severity.INFO, position)
        live = {p.ticket for p in positions}
        self._discovered_tickets &= live
        for ticket in [t for t in self._position_fingerprints if t not in live]:
            del self._position_fingerprints[ticket]
        await self._reconcile_outcomes(positions)
        return positions

    async def _reconcile_outcomes(self, positions: list[PositionInfo]) -> None:
        """Feed broker truth to the recorder and finalize records whose position is gone.

        Closing-deal details (real money and the true exit price) are fetched only for tickets
        that need finalizing, so the normal path costs nothing.
        """
        recorder = self.outcome_recorder
        if recorder is None:
            return
        try:
            live = {recorder.key_for(position) for position in positions}
            known = {
                (record.broker_ticket or record.trade_id) for record in recorder.all_outcomes()
            }
            for position in positions:
                key = recorder.key_for(position)
                if key in known:
                    continue  # already tracked (reconcile() below refreshes excursions)
                context = self._entry_context.get((position.symbol, position.side.value), {})
                recorder.observe_position(
                    position,
                    tick_size=self._symbol_tick.get(position.symbol),
                    strategy=str(context.get("strategy", "")),
                    strategy_version=str(context.get("strategy_version", "")),
                    timeframe=str(context.get("timeframe", self.timeframe)),
                    signal=context.get("signal"),
                    thesis=context.get("thesis"),
                    risk_decision=context.get("risk_decision"),
                    evidence=context.get("evidence"),
                    entry_context_id=str(context.get("entry_context_id", "")),
                    thesis_id=str(context.get("thesis_id", "")),
                    order_id=str(context.get("order_id", "")),
                    candles=list(self._bar_history.get(position.symbol, ())),
                )
            orphans = [
                record.broker_ticket or record.trade_id
                for record in recorder.open_outcomes()
                if (record.broker_ticket or record.trade_id) not in live
            ]
            details = await self._close_details_for(orphans)
            recorder.reconcile(positions, tick_sizes=dict(self._symbol_tick), close_details=details)
        except Exception as exc:
            self.stats.errors += 1
            self._audit(
                Severity.WARNING,
                {"stage": "outcome_reconcile_failed", "error": str(exc)},
            )

    async def _close_details_for(self, tickets: list[str]) -> dict[str, dict[str, Any]]:
        """Ask the adapter for closing-deal details (best effort, bounded by open records)."""
        getter = getattr(self.adapter, "position_close_details", None)
        if not callable(getter) or not tickets:
            return {}
        details: dict[str, dict[str, Any]] = {}
        for ticket in tickets:
            try:
                detail = await getter(ticket)
            except Exception:
                detail = None
            if detail:
                details[ticket] = dict(detail)
        return details

    async def _track_outcomes(self) -> None:
        """Track excursions from live quotes, then persist whatever the recorder queued."""
        recorder = self.outcome_recorder
        if recorder is None:
            return
        try:
            tracked = recorder.open_outcomes()
            if tracked:
                for symbol in {record.instrument for record in tracked}:
                    quote = await self.feed.quote(symbol)
                    bid = getattr(quote, "bid", None)
                    ask = getattr(quote, "ask", None)
                    price = (bid + ask) / 2.0 if bid and ask else bid
                    if price is None:
                        continue
                    for record in tracked:
                        if record.instrument == symbol:
                            recorder.observe_price(
                                record.broker_ticket or record.trade_id, price
                            )
            await recorder.flush()
        except Exception as exc:
            self.stats.errors += 1  # recording never breaks trading
            self._audit(
                Severity.WARNING,
                {"stage": "outcome_track_failed", "error": str(exc)},
            )

    async def _run_intelligence(self, quote: Any) -> None:
        """Feed the tick, (optionally) emit thesis signals, then manage the open positions.

        Thesis-derived signals take the normal `_handle_signal()` path and exist only when
        ``INTELLIGENCE_ENTRIES_ENABLED`` is set, because the strategy registry is the entry source
        of record. Position management below is independent of that gate, so in-trade decisions
        (trailing, break-even, reductions, exits) are dynamic whenever intelligence is enabled.
        Nothing here talks to the broker directly: every position decision goes through
        `_manage_position()` (PositionManager -> RiskEngine -> OrderManager -> adapter).
        """
        try:
            ctx = self.intelligence.feed_tick(bid=quote.bid, ask=quote.ask)
            if ctx is None or not ctx.usable_for_trading:
                return
            if bool(getattr(self.settings, "intelligence_entries_enabled", False)):
                thesis = self.intelligence.produce_thesis(ctx)
                if thesis is not None:
                    signal = self.intelligence.thesis_to_signal(thesis)
                    if signal is not None:
                        self.stats.signals += 1
                        account = await self.adapter.get_account()
                        await self._handle_signal(signal, account, quote)
            await self._evaluate_positions(ctx)
        except Exception:
            if self.intelligence is not None:
                self.intelligence.stats.errors += 1
            self._note_position("error", None, None, reason="intelligence_exception")

    def _note_position(
        self,
        outcome: str,
        ticket: str | None,
        action: Any | None,
        *,
        reason: str = "",
    ) -> None:
        """Trace in-trade management so the dashboard can explain what happened and why."""
        action_name = getattr(action, "value", None) or (str(action) if action else "")
        self._position_counts[f"outcome:{outcome}"] += 1
        if action_name:
            self._position_counts[f"action:{action_name}"] += 1
        self._position_management = {
            "at": utc_now().isoformat(),
            "outcome": outcome,
            "ticket": ticket,
            "action": action_name or None,
            "reason": reason,
            "entries_enabled": bool(
                getattr(self.settings, "intelligence_entries_enabled", False)
            ),
        }

    def _strategy_version(self, name: str) -> str:
        """Version of the strategy that produced a signal (promotion version id when promoted)."""
        strategy = None
        engine = self.signal_engine
        getter = getattr(engine, "get_strategy", None)
        if callable(getter):
            strategy = getter(name)
        if strategy is None:
            return ""
        return str(getattr(strategy, "version", "") or "")

    @property
    def news_state(self) -> dict[str, Any]:
        """Last news ingestion/blackout state (empty until a provider is configured)."""
        return dict(self._news_state)

    async def _poll_news(self) -> None:
        """Refresh the macro calendar. Never fatal: trading does not depend on the news feed."""
        ingestor = self.news_ingestor
        if ingestor is None:
            return
        try:
            result = await ingestor.run()
            self._last_news_poll = self._clock()
            self._news_state = {
                "at": utc_now().isoformat(),
                "ingest": result.to_dict(),
                "ingestor": ingestor.stats(),
            }
        except Exception as exc:
            self.stats.errors += 1
            self._audit(
                Severity.WARNING,
                {"stage": "news_ingest_failed", "error": str(exc)},
            )

    def _news_currencies(self) -> list[str]:
        from mt5_platform.ingestion.news import currencies_for_symbol

        found: list[str] = []
        for symbol in self.symbols:
            for currency in currencies_for_symbol(symbol):
                if currency not in found:
                    found.append(currency)
        return found

    async def _news_blackout_for(self, symbol: str) -> dict[str, Any]:
        """Scheduled high-impact news state for one instrument (opt-in; off unless enabled)."""
        from mt5_platform.ingestion.news import blackout_state, currencies_for_symbol

        enabled = bool(getattr(self.settings, "news_blackout_enabled", False))
        currencies = currencies_for_symbol(symbol)
        base: dict[str, Any] = {"enabled": enabled, "currencies": currencies}
        if not enabled or self.news_ingestor is None or not currencies:
            return {**base, "blocked": False, "reason": ""}
        events = await self.news_ingestor.recent(currencies=currencies, hours=24.0)
        state = blackout_state(
            events,
            now=utc_now(),
            currencies=currencies,
            before_minutes=float(self.settings.news_blackout_before_min),
            after_minutes=float(self.settings.news_blackout_after_min),
            min_impact=str(self.settings.news_blackout_min_impact),
        )
        return {**base, **state.to_dict(), "events_considered": len(events)}

    @property
    def position_management(self) -> dict[str, Any]:
        """Latest in-trade decision plus counters (empty until intelligence is enabled)."""
        return {
            "last": dict(self._position_management),
            "counts": dict(sorted(self._position_counts.items())),
            "intelligence": (
                self.intelligence.stats.to_dict() if self.intelligence is not None else None
            ),
            "entries_enabled": bool(
                getattr(self.settings, "intelligence_entries_enabled", False)
            ),
        }

    async def _evaluate_positions(self, ctx: Any) -> None:
        """Evaluate the positions this policy allows, then act on each decision."""
        policy = self.settings.manual_position_policy
        positions = await self.adapter.get_positions()
        if policy is ManualPositionPolicy.IGNORE:
            # Discovered and audited above, but never evaluated and never executed.
            for position in [p for p in positions if p.is_external]:
                self._position_event(
                    AuditEventType.POSITION_HOLD,
                    Severity.INFO,
                    position,
                    {
                        "policy": policy.value,
                        "managed": False,
                        "reason": "manual_position_policy=ignore",
                    },
                )
            positions = [p for p in positions if not p.is_external]
        if not positions:
            return
        account = await self.adapter.get_account()
        decisions = self.intelligence.evaluate_positions(positions, ctx)
        self._position_counts["evaluated"] += len(decisions)
        for position, decision in decisions:
            await self._manage_position(position, decision, account)

    async def _manage_position(self, position: PositionInfo, decision: Any, account: Any) -> None:
        """Route one PositionManager decision: RiskEngine -> OrderManager -> adapter.

        `manual_position_policy` decides what may happen to a position this bot did not open:
        ignore (never evaluated), observe (evaluated and audited, execution refused) and
        manage (executed like a bot-owned position, risk gate included).
        """
        policy = self.settings.manual_position_policy
        action = PositionDecision(decision.decision)
        context = {
            "policy": policy.value,
            "decision": action.value,
            "reason": decision.reason,
            "thesis_id": decision.original_thesis_id or None,
            "managed": not position.is_external or policy is ManualPositionPolicy.MANAGE,
        }
        if action in (PositionDecision.HOLD, PositionDecision.NO_ACTION):
            self._position_event(AuditEventType.POSITION_HOLD, Severity.INFO, position, context)
            self._note_position("hold", position.ticket, action, reason=decision.reason)
            return
        self._position_event(AuditEventType.POSITION_MONITORED, Severity.INFO, position, context)
        if position.is_external and policy is not ManualPositionPolicy.MANAGE:
            self._position_event(
                AuditEventType.POSITION_EXIT_REJECTED,
                Severity.INFO,
                position,
                {**context, "blocked_by": f"manual_position_policy={policy.value}"},
            )
            self._note_position(
                "policy_blocked",
                position.ticket,
                action,
                reason=f"manual_position_policy={policy.value}",
            )
            return
        self._position_event(
            AuditEventType.POSITION_EXIT_REQUESTED, Severity.WARNING, position, context
        )
        risk, record = await self.order_manager.execute_position_decision(
            position=position,
            decision=decision,
            account=account,
            adapter=self.adapter,
        )
        if record is None:
            self._position_event(
                AuditEventType.POSITION_EXIT_REJECTED,
                Severity.WARNING,
                position,
                {**context, "risk_approved": False, "risk_reasons": risk.reasons},
            )
            self._note_position(
                "risk_refused", position.ticket, action, reason=",".join(risk.reasons)
            )
            return
        executed = (
            AuditEventType.POSITION_MODIFIED
            if action is PositionDecision.MODIFY
            else AuditEventType.POSITION_REDUCED
            if action is PositionDecision.REDUCE
            else AuditEventType.POSITION_CLOSED
        )
        self._position_event(
            executed,
            Severity.INFO,
            position,
            {**context, "risk_approved": True, "execution": record.model_dump(mode="json")},
        )
        await self._record_position_decision(position, action, decision, record)
        self._note_position("executed", position.ticket, action, reason=decision.reason)
        if self.intelligence is not None and action in (
            PositionDecision.EXIT,
            PositionDecision.EMERGENCY_EXIT,
        ):
            self.intelligence.stats.position_exits += 1

    async def _record_position_decision(
        self,
        position: PositionInfo,
        action: PositionDecision,
        decision: Any,
        execution: Any,
    ) -> None:
        """Tell the recorder what this runtime just did, with an explicit exit cause.

        The cause is never inferred from profit/loss: it comes from the decision that performed the
        exit. Realized money is taken from the execution response, or from the broker's closing
        deals when the response does not carry it (that is where the truth is).
        """
        recorder = self.outcome_recorder
        if recorder is None:
            return
        try:
            key = recorder.key_for(position)
            response = dict(getattr(execution, "mt5_response", None) or {})
            realized = response.get("realized_pnl")
            commission = response.get("commission")
            swap = response.get("swap")
            deal = str(response.get("deal") or "") or None
            exit_price = getattr(execution, "execution_price", None) or position.current_price
            cause, cause_source = cause_from_position_decision(decision)
            reason = decision.reason or "position_manager"
            if realized is None and action in (
                PositionDecision.EXIT,
                PositionDecision.EMERGENCY_EXIT,
            ):
                # The terminal response often omits money; the closing deal has it.
                details = (await self._close_details_for([key])).get(key) or {}
                realized = details.get("realized_pnl")
                commission = details.get("commission")
                swap = details.get("swap")
                if details.get("exit_price") is not None:
                    exit_price = details["exit_price"]
                if details.get("deals"):
                    deal = ",".join(str(d) for d in details["deals"])[:60]
            if action is PositionDecision.MODIFY:
                recorder.record_modification(
                    key,
                    stop_loss=getattr(execution, "stop_loss", None) or position.stop_loss,
                    take_profit=getattr(execution, "take_profit", None) or position.take_profit,
                    reason=reason,
                )
            elif action is PositionDecision.REDUCE:
                recorder.record_partial_close(
                    key,
                    volume=float(getattr(execution, "requested_volume", 0.0) or 0.0),
                    price=getattr(execution, "execution_price", None) or position.current_price,
                    realized_pnl=float(realized) if realized is not None else None,
                    commission=float(commission) if commission is not None else None,
                    swap=float(swap) if swap is not None else None,
                    cause=cause,
                    reason=reason,
                    broker_deal=deal,
                )
            elif action in (PositionDecision.EXIT, PositionDecision.EMERGENCY_EXIT):
                recorder.finalize(
                    key,
                    exit_price=exit_price,
                    cause=cause,
                    cause_source=cause_source,
                    realized_pnl=float(realized) if realized is not None else None,
                    commission=float(commission) if commission is not None else None,
                    swap=float(swap) if swap is not None else None,
                    reason=reason,
                    broker_deal=deal,
                )
        except Exception as exc:
            self.stats.errors += 1
            self._audit(
                Severity.WARNING,
                {"stage": "outcome_record_decision_failed", "error": str(exc)},
            )

    def _remember_entry_context(
        self, signal: StrategySignal, record: ExecutionRecord, decision: Any
    ) -> None:
        """Keep the entry context so the recorder can attribute a new position to its signal.

        Keyed by (symbol, side): broker truth only tells us symbol/side, and the newest filled
        entry for that pair is the position we just opened.
        """
        if record.final_status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return
        metadata = dict(signal.metadata or {})
        thesis = metadata.get("thesis") if isinstance(metadata.get("thesis"), dict) else {}
        self._entry_context[(signal.symbol, signal.direction.value)] = {
            "strategy": signal.strategy_name,
            "strategy_version": str(metadata.get("strategy_version", ""))
            or self._strategy_version(signal.strategy_name),
            "timeframe": str(metadata.get("timeframe", self.timeframe)),
            "signal": signal,
            "thesis": thesis or {},
            "thesis_id": str(metadata.get("thesis_id", "")),
            "entry_context_id": str(metadata.get("context_id", "")),
            "order_id": record.order_id or "",
            "risk_decision": {
                "approved": bool(decision.approved),
                "reasons": list(decision.reasons),
            },
            "evidence": {
                "evidence_quality": str(metadata.get("evidence_quality", "")),
                "signal_confidence": signal.confidence,
            },
        }

    @staticmethod
    def _signal_ref(signal: StrategySignal) -> dict[str, Any]:
        """Compact identity of a signal for the pipeline trace."""
        return {
            "strategy": signal.strategy_name,
            "symbol": signal.symbol,
            "side": signal.direction.value,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "confidence": signal.confidence,
            "signal_id": signal.signal_id,
            "reason": signal.reason,
        }

    @staticmethod
    def _position_payload(position: PositionInfo) -> dict[str, Any]:
        """Everything the dashboard and the audit trail need to identify a broker position."""
        return {
            "ticket": position.ticket,
            "symbol": position.symbol,
            "side": position.side.value,
            "volume": position.volume,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "floating_pnl": position.floating_pnl,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "opened_at": position.opened_at.isoformat(),
            "external": position.is_external,
            "magic": position.magic,
            "comment": position.comment,
        }

    @classmethod
    def _position_event(
        cls,
        event_type: AuditEventType,
        severity: Severity,
        position: PositionInfo,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """One lifecycle audit event, always carrying the full broker position snapshot."""
        audit_log.emit(
            AuditEvent(
                component="position_lifecycle",
                event_type=event_type.value,
                severity=severity,
                symbol=position.symbol,
                correlation_id=position.ticket,
                payload={**cls._position_payload(position), **(extra or {})},
            )
        )

    async def _handle_signal(self, signal: StrategySignal, account, quote) -> None:
        if quote is None:
            self.stats.skipped["no_quote"] += 1
            self._stage("signal_not_actionable", reason="no_quote", signal=self._signal_ref(signal))
            return
        spec = await self.adapter.get_instrument(signal.symbol)
        if spec is None:
            self.stats.skipped["no_instrument_spec"] += 1
            self._stage(
                "signal_not_actionable",
                reason="no_instrument_spec",
                signal=self._signal_ref(signal),
            )
            return
        if signal.stop_loss is None:
            self.stats.skipped["no_stop_loss"] += 1
            self._stage(
                "signal_not_actionable", reason="no_stop_loss", signal=self._signal_ref(signal)
            )
            return
        positions = await self.adapter.get_positions()
        est_entry = quote.ask if signal.direction is OrderSide.BUY else quote.bid
        blackout = await self._news_blackout_for(signal.symbol)
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
            self._stage(
                "signal_not_actionable",
                reason="size_too_small",
                equity=account.equity,
                risk_pct=self.risk_pct,
                signal=self._signal_ref(signal),
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
            news_blackout=bool(blackout.get("blocked", False)),
            news_blackout_reason=str(blackout.get("reason", "")),
        )
        _, decision, record = await self.order_manager.process_signal(signal, ctx, self.adapter)
        if record is not None:
            self.stats.orders_sent += 1
            # Stages E/F/G: risk approved, broker was asked, outcome recorded.
            self._stage_order(signal, record, decision)
            self._remember_entry_context(signal, record, decision)
        elif not decision.approved:
            for reason in decision.reasons:
                self.stats.skipped[f"risk:{reason}"] += 1
            # Stage D: a signal existed, but the risk gate refused it.
            self._stage(
                "signal_rejected_by_risk",
                reasons=decision.reasons,
                volume=volume,
                spread_points=quote.spread_points,
                data_age_ms=quote.age_ms,
                open_positions=len(positions),
                signal=self._signal_ref(signal),
            )

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
