"""Mandatory risk engine. Failed checks mean DO NOT TRADE.

Phase 5 — expanded coverage: position size, risk-per-trade, exposure,
slippage, margin level, account sanity, signal-level sanity, pause/kill.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderSide, Severity
from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    OrderRequest,
    RiskDecision,
    StrategySignal,
)
from mt5_platform.common.ids import new_signal_id
from mt5_platform.config import Settings


@dataclass
class RiskContext:
    account: AccountSnapshot
    open_positions: int = 0
    current_spread: float | None = None
    data_age_ms: float | None = None
    duplicate_position: bool = False
    market_session_ok: bool = True
    stop_loss_required: bool = True
    proposed_volume: float | None = None
    current_exposure: float | None = None
    current_slippage: float | None = None


@dataclass
class RiskEngineStats:
    checks: int = 0
    approved: int = 0
    rejected: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        rate = round(self.approved / self.checks, 4) if self.checks else None
        return {
            "checks": self.checks,
            "approved": self.approved,
            "rejected": self.rejected,
            "approval_rate": rate,
            "reject_reasons": dict(sorted(self.reject_reasons.items())),
        }


@dataclass
class RiskEngine:
    settings: Settings
    kill_switch: bool = False
    paused: bool = False
    _halt_reasons: list[str] = field(default_factory=list)
    _pause_reasons: list[str] = field(default_factory=list)
    _stats: RiskEngineStats = field(default_factory=RiskEngineStats)
    _max_decisions: int = 200
    _decisions: deque[RiskDecision] = field(default_factory=deque)

    @property
    def halt_reasons(self) -> list[str]:
        return list(self._halt_reasons)

    @property
    def pause_reasons(self) -> list[str]:
        return list(self._pause_reasons)

    def engage_kill_switch(self, reason: str) -> None:
        self.kill_switch = True
        self._halt_reasons.append(reason)
        audit_log.emit(
            AuditEvent(
                component="risk",
                event_type=AuditEventType.EMERGENCY_STOP.value,
                severity=Severity.CRITICAL,
                payload={"reason": reason},
            )
        )

    def release_kill_switch(self) -> None:
        self.kill_switch = False
        audit_log.emit(
            AuditEvent(
                component="risk",
                event_type=AuditEventType.RISK_KILL_SWITCH_RELEASED.value,
                severity=Severity.INFO,
                payload={"reason": "manual_release"},
            )
        )

    def pause_trading(self, reason: str) -> None:
        self.paused = True
        self._pause_reasons.append(reason)
        audit_log.emit(
            AuditEvent(
                component="risk",
                event_type=AuditEventType.RISK_TRADING_PAUSED.value,
                severity=Severity.WARNING,
                payload={"reason": reason},
            )
        )

    def resume_trading(self) -> None:
        self.paused = False
        audit_log.emit(
            AuditEvent(
                component="risk",
                event_type=AuditEventType.RISK_TRADING_RESUMED.value,
                severity=Severity.INFO,
                payload={"reason": "manual_resume"},
            )
        )

    def evaluate(self, signal: StrategySignal, ctx: RiskContext) -> RiskDecision:
        account = ctx.account
        reasons: list[str] = []

        if self.kill_switch or not self.settings.trading_allowed:
            reasons.append("emergency_kill_switch")
        if self.paused:
            reasons.append("trading_paused")
        if self.settings.is_live and not (
            self.settings.live_trading_enabled and self.settings.live_trading_acknowledged
        ):
            reasons.append("live_trading_not_acknowledged")

        self._check_account_state(account, reasons)
        self._check_signal_levels(signal, reasons)

        if ctx.open_positions >= self.settings.max_simultaneous_positions:
            reasons.append("max_simultaneous_positions")
        if ctx.duplicate_position:
            reasons.append("duplicate_position")
        if ctx.current_spread is not None and ctx.current_spread > self.settings.max_spread_points:
            reasons.append("spread_too_wide")
        if (
            ctx.current_slippage is not None
            and ctx.current_slippage > self.settings.max_slippage_points
        ):
            reasons.append("slippage_too_high")
        if ctx.data_age_ms is not None and ctx.data_age_ms > self.settings.stale_data_max_age_ms:
            reasons.append("stale_data_protection")
        if not ctx.market_session_ok:
            reasons.append("market_session_closed")
        if ctx.stop_loss_required and signal.stop_loss is None:
            reasons.append("stop_loss_required")

        if account.free_margin <= 0:
            reasons.append("insufficient_margin")
        if account.drawdown_pct >= self.settings.max_drawdown_pct:
            reasons.append("max_drawdown_exceeded")
        if account.balance > 0 and abs(account.daily_pnl) > 0:
            daily_loss_pct = max(0.0, -account.daily_pnl / account.balance * 100.0)
            if daily_loss_pct >= self.settings.max_daily_loss_pct:
                reasons.append("max_daily_loss")
        if (
            account.margin_level is not None
            and account.margin_level < self.settings.min_margin_level_pct
        ):
            reasons.append("margin_level_too_low")

        if ctx.proposed_volume is not None:
            self._check_position_limits(signal, ctx, account, reasons)

        approved = len(reasons) == 0
        decision = RiskDecision(
            approved=approved, reasons=reasons, correlation_id=signal.correlation_id
        )
        self._stats.checks += 1
        if approved:
            self._stats.approved += 1
        else:
            self._stats.rejected += 1
            for reason in reasons:
                self._stats.reject_reasons[reason] = self._stats.reject_reasons.get(reason, 0) + 1
        self._decisions.append(decision)
        if len(self._decisions) > self._max_decisions:
            self._decisions.popleft()

        audit_log.emit(
            AuditEvent(
                component="risk",
                event_type=(
                    AuditEventType.RISK_APPROVED.value
                    if approved
                    else AuditEventType.RISK_CHECK_FAILED.value
                ),
                severity=Severity.INFO if approved else Severity.WARNING,
                symbol=signal.symbol,
                correlation_id=signal.correlation_id,
                payload={
                    "reasons": reasons,
                    "signal_id": signal.signal_id,
                    "proposed_volume": ctx.proposed_volume,
                },
            )
        )
        return decision

    def _check_account_state(self, account: AccountSnapshot, reasons: list[str]) -> None:
        money = (
            account.balance,
            account.equity,
            account.free_margin,
            account.used_margin,
            account.floating_pnl,
        )
        if any(not math.isfinite(float(v)) for v in money):
            reasons.append("invalid_account_state")
            return
        if account.equity <= 0:
            reasons.append("insufficient_equity")
        if account.balance < 0:
            reasons.append("negative_balance")

    @staticmethod
    def _check_signal_levels(signal: StrategySignal, reasons: list[str]) -> None:
        entry = signal.entry
        if entry is not None and entry <= 0:
            reasons.append("invalid_entry")
        if signal.stop_loss is not None:
            if signal.stop_loss <= 0:
                reasons.append("invalid_stop_loss")
            elif entry is not None and entry > 0:
                if signal.direction is OrderSide.BUY and signal.stop_loss >= entry:
                    reasons.append("stop_loss_wrong_side")
                if signal.direction is OrderSide.SELL and signal.stop_loss <= entry:
                    reasons.append("stop_loss_wrong_side")
        if signal.take_profit is not None:
            if signal.take_profit <= 0:
                reasons.append("invalid_take_profit")
            elif entry is not None and entry > 0:
                if signal.direction is OrderSide.BUY and signal.take_profit <= entry:
                    reasons.append("take_profit_wrong_side")
                if signal.direction is OrderSide.SELL and signal.take_profit >= entry:
                    reasons.append("take_profit_wrong_side")

    def _check_position_limits(
        self,
        signal: StrategySignal,
        ctx: RiskContext,
        account: AccountSnapshot,
        reasons: list[str],
    ) -> None:
        volume = ctx.proposed_volume
        if volume is None:
            return
        if volume <= 0:
            reasons.append("invalid_volume")
            return
        if volume > self.settings.max_position_size:
            reasons.append("max_position_size")

        entry = signal.entry or 0.0
        equity = account.equity
        if equity > 0 and entry > 0 and signal.stop_loss is not None:
            risk_amount = abs(entry - signal.stop_loss) * volume
            risk_pct = risk_amount / equity * 100.0
            if risk_pct > self.settings.max_risk_per_trade_pct:
                reasons.append("max_risk_per_trade")

        if equity > 0 and entry > 0:
            current = ctx.current_exposure if ctx.current_exposure is not None else account.exposure
            exposure_pct = (current + volume * entry) / equity * 100.0
            if exposure_pct > self.settings.max_exposure_pct:
                reasons.append("max_exposure")

    def evaluate_order(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        signal = StrategySignal(
            signal_id=order.signal_id or new_signal_id(),
            symbol=order.symbol,
            direction=order.side,
            entry=order.entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            confidence=1.0,
            reason="order_risk_check",
            timestamp=order.created_at,
            strategy_name="order_manager",
            correlation_id=order.correlation_id,
        )
        ctx.proposed_volume = order.volume
        return self.evaluate(signal, ctx)

    def snapshot(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "paused": self.paused,
            "halt_reasons": self.halt_reasons,
            "pause_reasons": self.pause_reasons,
            "settings": {
                "max_position_size": self.settings.max_position_size,
                "max_risk_per_trade_pct": self.settings.max_risk_per_trade_pct,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "max_drawdown_pct": self.settings.max_drawdown_pct,
                "max_simultaneous_positions": self.settings.max_simultaneous_positions,
                "max_spread_points": self.settings.max_spread_points,
                "max_slippage_points": self.settings.max_slippage_points,
                "max_exposure_pct": self.settings.max_exposure_pct,
                "min_margin_level_pct": self.settings.min_margin_level_pct,
                "stale_data_max_age_ms": self.settings.stale_data_max_age_ms,
                "trading_allowed": self.settings.trading_allowed,
            },
            "stats": self._stats.to_dict(),
            "recent_decisions": [
                {
                    "approved": d.approved,
                    "reasons": d.reasons,
                    "correlation_id": d.correlation_id,
                    "checked_at": d.checked_at.isoformat(),
                }
                for d in list(self._decisions)[-20:]
            ],
        }


__all__ = ["RiskContext", "RiskEngine", "RiskEngineStats"]
