"""Signal engine — runs strategies, audits, persists via sinks. Never places orders."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, Severity
from mt5_platform.common.events import AuditEvent, MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy

SignalSink = Callable[[StrategySignal], Awaitable[None] | None]
AuditSink = Callable[[AuditEvent], Awaitable[None] | None]


@dataclass
class SignalEngineStats:
    events_processed: int = 0
    signals_generated: int = 0
    signals_rejected: int = 0
    strategy_errors: int = 0
    sink_errors: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    # Warm-up/replay activity is counted separately: it is never traded and never persisted.
    replay_evaluations: int = 0
    replay_signals: int = 0
    replay_rejections: int = 0

    def to_dict(self) -> dict:
        return {
            "events_processed": self.events_processed,
            "signals_generated": self.signals_generated,
            "signals_rejected": self.signals_rejected,
            "strategy_errors": self.strategy_errors,
            "sink_errors": self.sink_errors,
            "reject_reasons": dict(sorted(self.reject_reasons.items())),
            "replay_evaluations": self.replay_evaluations,
            "replay_signals": self.replay_signals,
            "replay_rejections": self.replay_rejections,
        }


class SignalEngine:
    """Orchestrates strategies. Does not call RiskEngine, OrderManager, or MT5."""

    def __init__(
        self,
        strategies: list[Strategy] | None = None,
        *,
        min_confidence: float = 0.0,
        require_stop_loss: bool = True,
        cooldown_s: float = 0.0,
        sink: SignalSink | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.strategies = list(strategies or [])
        self.min_confidence = min_confidence
        self.require_stop_loss = require_stop_loss
        self.cooldown_s = max(0.0, cooldown_s)
        self.sink = sink
        self.audit_sink = audit_sink
        self.stats = SignalEngineStats()
        self.strategy_stats: dict[str, SignalEngineStats] = {
            s.name: SignalEngineStats() for s in self.strategies
        }
        self._recent: list[StrategySignal] = []
        self._max_recent = 500
        self._last_emitted: dict[tuple[str, str, str], datetime] = {}
        # Diagnostics: "0 signals" must be explainable, never a bare counter.
        self.evaluations: int = 0
        self.last_evaluation_time: datetime | None = None
        self.last_evaluation_symbol: str | None = None
        self.last_signal: StrategySignal | None = None
        self.last_rejection: dict[str, object] | None = None
        # Historical warm-up/replay activity, kept strictly apart from live trading state.
        self.replay_evaluations: int = 0
        self.replay_signals_generated: int = 0
        self.replay_signals_rejected: int = 0
        self._replay_signals: list[StrategySignal] = []
        self._max_replay = 200

    @staticmethod
    def _bump(stats: SignalEngineStats | None, **fields: int) -> None:
        if stats is None:
            return
        for name, value in fields.items():
            setattr(stats, name, getattr(stats, name) + value)

    def register(self, strategy: Strategy) -> None:
        self.strategies.append(strategy)
        self.strategy_stats.setdefault(strategy.name, SignalEngineStats())

    def get_strategy(self, name: str) -> Strategy | None:
        for strategy in self.strategies:
            if strategy.name == name:
                return strategy
        return None

    async def enable(self, name: str) -> Strategy | None:
        strategy = self.get_strategy(name)
        if strategy is None:
            return None
        strategy.enabled = True
        await self._emit_audit(
            AuditEvent(
                component="signal_engine",
                event_type=AuditEventType.STRATEGY_ENABLED.value,
                severity=Severity.INFO,
                payload={"strategy": name},
            )
        )
        return strategy

    async def disable(self, name: str) -> Strategy | None:
        strategy = self.get_strategy(name)
        if strategy is None:
            return None
        strategy.enabled = False
        await self._emit_audit(
            AuditEvent(
                component="signal_engine",
                event_type=AuditEventType.STRATEGY_DISABLED.value,
                severity=Severity.INFO,
                payload={"strategy": name},
            )
        )
        return strategy

    def list_strategies(self) -> list[dict]:
        return [
            {
                **strategy.info(),
                "stats": self.strategy_stats.get(strategy.name, SignalEngineStats()).to_dict(),
            }
            for strategy in self.strategies
        ]

    def recent_signals(self, limit: int = 50) -> list[StrategySignal]:
        if limit <= 0:
            return []
        return self._recent[-limit:]

    def replay_signals(self, limit: int = 50) -> list[StrategySignal]:
        """Signals produced while replaying history: tagged, never traded, never persisted."""
        if limit <= 0:
            return []
        return list(self._replay_signals[-limit:])

    def reset_state(self) -> None:
        """Reset strategy/cooldown state before a fresh runtime warm-up.

        Counters and persisted recent signals are intentionally retained for observability.
        Replay diagnostics are per warm-up session, so they restart with the session.
        """
        for strategy in self.strategies:
            strategy.reset()
        self._last_emitted.clear()
        self.replay_evaluations = 0
        self.replay_signals_generated = 0
        self.replay_signals_rejected = 0
        self._replay_signals.clear()
        for stats in self.strategy_stats.values():
            stats.replay_evaluations = 0
            stats.replay_signals = 0
            stats.replay_rejections = 0

    def stats_snapshot(self) -> dict:
        """Everything needed to explain why a signal did or did not happen."""
        strategies: dict[str, dict] = {}
        for name, stats in sorted(self.strategy_stats.items()):
            data = stats.to_dict()
            data["evaluations"] = data["events_processed"]
            data["signals"] = data["signals_generated"]
            data["rejections"] = data["signals_rejected"]
            strategies[name] = data
        last = self.last_signal
        return {
            **self.stats.to_dict(),
            "evaluations": self.evaluations,
            # Warm-up/replay activity: reported, but never mixed into live numbers or the store.
            "replay_evaluations": self.replay_evaluations,
            "replay_signals_generated": self.replay_signals_generated,
            "replay_signals_rejected": self.replay_signals_rejected,
            "replay_persisted": False,
            "last_evaluation_time": (
                self.last_evaluation_time.isoformat() if self.last_evaluation_time else None
            ),
            "last_evaluation_symbol": self.last_evaluation_symbol,
            "last_signal_time": last.timestamp.isoformat() if last else None,
            "last_signal_strategy": last.strategy_name if last else None,
            "last_signal_side": last.direction.value if last else None,
            "last_signal_reason": last.reason if last else None,
            "last_signal_entry": last.entry if last else None,
            "last_signal_stop_loss": last.stop_loss if last else None,
            "last_signal_take_profit": last.take_profit if last else None,
            "last_signal_confidence": last.confidence if last else None,
            "last_rejection": dict(self.last_rejection) if self.last_rejection else None,
            "cooldown_s": self.cooldown_s,
            "min_confidence": self.min_confidence,
            "require_stop_loss": self.require_stop_loss,
            "total_strategies": len(self.strategies),
            "active_strategies": sum(1 for s in self.strategies if s.enabled),
            "strategy_stats": strategies,
        }

    async def on_market_data(
        self, event: MarketDataEvent, *, replay: bool = False
    ) -> list[StrategySignal]:
        """Evaluate one candle.

        ``replay=True`` marks a historical warm-up candle: it is counted separately, tagged,
        kept out of the operational signal feed and **never** handed to the store sinks or the
        live cooldown/state, so a replay can never look like live trading.
        """
        if replay:
            self.replay_evaluations += 1
        else:
            self._bump(self.stats, events_processed=1)
            self.evaluations += 1
            self.last_evaluation_time = event.timestamp
            self.last_evaluation_symbol = event.symbol
        emitted: list[StrategySignal] = []

        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            per = self.strategy_stats.setdefault(strategy.name, SignalEngineStats())
            if replay:
                self._bump(per, replay_evaluations=1)
            else:
                self._bump(per, events_processed=1)
            try:
                signal = strategy.generate_signal(event)
            except Exception as exc:
                self._bump(self.stats, strategy_errors=1)
                self._bump(per, strategy_errors=1)
                if not replay:
                    await self._emit_audit(
                        AuditEvent(
                            component="strategy",
                            event_type=AuditEventType.STRATEGY_ERROR.value,
                            severity=Severity.ERROR,
                            symbol=event.symbol,
                            correlation_id=event.correlation_id,
                            error=str(exc),
                            payload={"strategy": strategy.name},
                        )
                    )
                continue

            if signal is None:
                continue

            rejected = self._reject_reasons(signal)
            cooldown_key = (signal.strategy_name, signal.symbol, signal.direction.value)
            last = self._last_emitted.get(cooldown_key)
            if last is not None and (signal.timestamp - last).total_seconds() < self.cooldown_s:
                rejected.append("cooldown")

            if rejected:
                if replay:
                    self.replay_signals_rejected += 1
                    self._bump(per, replay_rejections=1)
                    continue
                self._bump(self.stats, signals_rejected=1)
                self.last_rejection = {
                    "at": signal.timestamp.isoformat(),
                    "strategy": signal.strategy_name,
                    "symbol": signal.symbol,
                    "side": signal.direction.value,
                    "reasons": list(rejected),
                    "signal_id": signal.signal_id,
                }
                self._bump(per, signals_rejected=1)
                for reason in rejected:
                    self.stats.reject_reasons[reason] = self.stats.reject_reasons.get(reason, 0) + 1
                    per.reject_reasons[reason] = per.reject_reasons.get(reason, 0) + 1
                await self._emit_audit(
                    AuditEvent(
                        component="signal_engine",
                        event_type=AuditEventType.SIGNAL_REJECTED.value,
                        severity=Severity.WARNING,
                        symbol=signal.symbol,
                        correlation_id=signal.correlation_id,
                        payload={
                            "strategy": signal.strategy_name,
                            "reasons": rejected,
                            "signal_id": signal.signal_id,
                        },
                    )
                )
                continue

            if replay:
                # Tagged, kept in a replay-only buffer, and never sent to risk/store/cooldown.
                self.replay_signals_generated += 1
                self._bump(per, replay_signals=1)
                self._replay_signals.append(
                    signal.model_copy(
                        update={
                            "metadata": {**signal.metadata, "session": "warmup", "replay": True}
                        }
                    )
                )
                if len(self._replay_signals) > self._max_replay:
                    self._replay_signals = self._replay_signals[-self._max_replay :]
                emitted.append(signal)
                continue

            self._bump(self.stats, signals_generated=1)
            self.last_signal = signal
            self._bump(per, signals_generated=1)
            self._last_emitted[cooldown_key] = signal.timestamp
            self._recent.append(signal)
            if len(self._recent) > self._max_recent:
                self._recent = self._recent[-self._max_recent :]
            emitted.append(signal)

            await self._emit_audit(
                AuditEvent(
                    component="signal_engine",
                    event_type=AuditEventType.SIGNAL_GENERATED.value,
                    severity=Severity.INFO,
                    symbol=signal.symbol,
                    correlation_id=signal.correlation_id,
                    payload={
                        "strategy": signal.strategy_name,
                        "direction": signal.direction.value,
                        "confidence": signal.confidence,
                        "signal_id": signal.signal_id,
                        "entry": signal.entry,
                        "stop_loss": signal.stop_loss,
                        "take_profit": signal.take_profit,
                    },
                )
            )

            if self.sink is not None:
                try:
                    maybe = self.sink(signal)
                    if maybe is not None and hasattr(maybe, "__await__"):
                        await maybe
                except Exception as exc:
                    self._bump(self.stats, sink_errors=1)
                    self._bump(per, sink_errors=1)
                    await self._emit_audit(
                        AuditEvent(
                            component="signal_engine",
                            event_type=AuditEventType.SINK_FAILED.value,
                            severity=Severity.ERROR,
                            symbol=signal.symbol,
                            correlation_id=signal.correlation_id,
                            error=str(exc),
                            payload={"sink": "signal_store", "signal_id": signal.signal_id},
                        )
                    )

        return emitted

    def _reject_reasons(self, signal: StrategySignal) -> list[str]:
        reasons: list[str] = []
        if signal.confidence < self.min_confidence:
            reasons.append("confidence_below_minimum")
        if self.require_stop_loss and signal.stop_loss is None:
            reasons.append("missing_stop_loss")
        if signal.entry is None:
            reasons.append("missing_entry")
        return reasons

    async def _emit_audit(self, event: AuditEvent) -> None:
        audit_log.emit(event)
        if self.audit_sink is None:
            return
        try:
            maybe = self.audit_sink(event)
            if maybe is not None and hasattr(maybe, "__await__"):
                await maybe
        except Exception as exc:
            audit_log.emit(
                AuditEvent(
                    component="signal_engine",
                    event_type=AuditEventType.SINK_FAILED.value,
                    severity=Severity.ERROR,
                    symbol=event.symbol,
                    correlation_id=event.correlation_id,
                    error=str(exc),
                    payload={"sink": "audit_store", "original_event": event.event_type},
                )
            )


class SignalStoreSink:
    """Async sink that writes each emitted signal to a MarketDataStore."""

    def __init__(self, store) -> None:
        self.store = store

    async def __call__(self, signal: StrategySignal) -> None:
        await self.store.write_signal(signal)


class AuditStoreSink:
    """Async sink that persists audit events to a MarketDataStore."""

    def __init__(self, store) -> None:
        self.store = store

    async def __call__(self, event: AuditEvent) -> None:
        await self.store.write_audit(event)


def build_signal_engine(settings, store) -> SignalEngine:
    """Build a configured engine from Settings + store, wiring persistence sinks."""
    from mt5_platform.strategy import NullStrategy, available_strategies, create_strategy

    strategies: list[Strategy] = []
    unknown: list[str] = []
    for name in settings.active_strategies:
        try:
            strategies.append(create_strategy(name))
        except ValueError:
            unknown.append(name)

    if not strategies:
        strategies.append(NullStrategy())

    # Research promotions are explicit: an approved-promotion file replaces the configured list for
    # the traded symbol, and every strategy carries the promotion version id for outcome tracing.
    promotion_path = str(getattr(settings, "promotion_config_path", "") or "").strip()
    if promotion_path:
        from mt5_platform.research.promotion import build_promoted_strategies

        promoted = build_promoted_strategies(
            promotion_path,
            symbol=getattr(settings, "default_symbol", None),
            timeframe=getattr(settings, "timeframe", None),
        )
        if promoted:
            strategies = list(promoted)
            audit_log.emit(
                AuditEvent(
                    component="signal_engine",
                    event_type=AuditEventType.STRATEGY_ENABLED.value,
                    severity=Severity.INFO,
                    payload={
                        "source": "promotion_config",
                        "path": promotion_path,
                        "promotions": [getattr(s, "promotion", {}) for s in promoted],
                    },
                )
            )

    if unknown:
        audit_log.emit(
            AuditEvent(
                component="signal_engine",
                event_type=AuditEventType.STRATEGY_CONFIG_INVALID.value,
                severity=Severity.WARNING,
                payload={"unknown_strategies": unknown, "available": available_strategies()},
            )
        )

    return SignalEngine(
        strategies,
        min_confidence=settings.signal_min_confidence,
        require_stop_loss=settings.signal_require_stop_loss,
        cooldown_s=settings.signal_cooldown_s,
        sink=SignalStoreSink(store) if settings.signal_store_sink_enabled else None,
        audit_sink=AuditStoreSink(store) if settings.signal_audit_store_sink_enabled else None,
    )


__all__ = [
    "AuditSink",
    "AuditStoreSink",
    "SignalEngine",
    "SignalEngineStats",
    "SignalSink",
    "SignalStoreSink",
    "build_signal_engine",
]
