"""Phase 5 risk-engine tests — expanded coverage, stats, kill/pause, API surface."""

from __future__ import annotations

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from mt5_platform.api import create_app
from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import OrderSide, OrderStatus
from mt5_platform.common.events import AccountSnapshot, OrderRequest, StrategySignal
from mt5_platform.config import Settings, TradingMode, clear_settings_cache
from mt5_platform.risk import RiskContext, RiskEngine


def _account(**overrides) -> AccountSnapshot:
    base: dict = {
        "balance": 10_000.0,
        "equity": 10_000.0,
        "free_margin": 9_500.0,
        "used_margin": 500.0,
        "floating_pnl": 0.0,
    }
    base.update(overrides)
    return AccountSnapshot(**base)


def _signal(
    *,
    side: OrderSide = OrderSide.BUY,
    entry: float = 2500.0,
    stop_loss: float = 2490.0,
    take_profit: float = 2520.0,
    confidence: float = 0.8,
    symbol: str = "XAUUSD",
) -> StrategySignal:
    return StrategySignal(
        symbol=symbol,
        direction=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
    )


def _engine(**settings_overrides) -> tuple[RiskEngine, Settings]:
    settings = Settings(trading_mode=TradingMode.DEMO, **settings_overrides)
    return RiskEngine(settings=settings), settings


def test_risk_engine_rejects_oversized_position() -> None:
    engine, _ = _engine(max_position_size=0.10)
    ctx = RiskContext(account=_account(), proposed_volume=5.0)
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "max_position_size" in decision.reasons


def test_risk_engine_rejects_zero_volume() -> None:
    engine, _ = _engine()
    ctx = RiskContext(account=_account(), proposed_volume=0.0)
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "invalid_volume" in decision.reasons


def test_risk_engine_rejects_high_risk_per_trade() -> None:
    engine, _ = _engine(max_risk_per_trade_pct=1.0)
    account = _account(equity=100.0, balance=100.0)
    ctx = RiskContext(account=account, proposed_volume=0.01)
    # Entry 2500 / stop 500 → 2000 risk * 0.01 = 20 on 100 equity = 20% > 1%
    signal = _signal(entry=2500.0, stop_loss=500.0)
    decision = engine.evaluate(signal, ctx)
    reasons = set(decision.reasons)
    assert "max_risk_per_trade" in reasons
    assert "stop_loss_wrong_side" not in reasons


def test_risk_engine_rejects_excess_exposure() -> None:
    engine, _ = _engine(max_exposure_pct=1000.0)
    account = _account(equity=1000.0, balance=1000.0)
    ctx = RiskContext(account=account, proposed_volume=0.01, current_exposure=10_000.0)
    # 10000 + 2500*0.01 = 10025 on 1000 equity = 1002.5% > 1000%
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "max_exposure" in decision.reasons


def test_risk_engine_rejects_wide_slippage() -> None:
    engine, _ = _engine(max_slippage_points=25.0)
    ctx = RiskContext(account=_account(), current_slippage=40.0)
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "slippage_too_high" in decision.reasons


def test_risk_engine_rejects_low_margin_level() -> None:
    engine, _ = _engine(min_margin_level_pct=20.0)
    account = _account(margin_level=15.0)
    ctx = RiskContext(account=account)
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "margin_level_too_low" in decision.reasons


def test_risk_engine_rejects_invalid_account_state() -> None:
    engine, _ = _engine()
    account = _account(equity=float("nan"))
    ctx = RiskContext(account=account)
    decision = engine.evaluate(_signal(), ctx)
    assert not decision.approved
    assert "invalid_account_state" in decision.reasons

    account2 = _account(equity=0.0)
    decision2 = engine.evaluate(_signal(), RiskContext(account=account2))
    assert "insufficient_equity" in decision2.reasons
def test_risk_engine_rejects_wrong_side_stop_and_take_profit() -> None:
    engine, _ = _engine()
    ctx = RiskContext(account=_account())

    bad_stop = _signal(side=OrderSide.BUY, entry=2500.0, stop_loss=2510.0)
    decision = engine.evaluate(bad_stop, ctx)
    assert "stop_loss_wrong_side" in decision.reasons

    bad_tp = _signal(side=OrderSide.SELL, entry=2500.0, stop_loss=2490.0, take_profit=2510.0)
    decision = engine.evaluate(bad_tp, ctx)
    assert "take_profit_wrong_side" in decision.reasons


def test_risk_engine_approves_clean_signal() -> None:
    engine, _ = _engine()
    ctx = RiskContext(account=_account(), proposed_volume=0.01)
    decision = engine.evaluate(_signal(), ctx)
    assert decision.approved
    assert decision.reasons == []
    assert engine._stats.approved == 1  # noqa: SLF001
    assert engine.snapshot()["stats"]["reject_reasons"] == {}


def test_risk_engine_pause_kill_and_stats() -> None:
    engine, _ = _engine()

    engine.pause_trading("review")
    ctx = RiskContext(account=_account())
    decision = engine.evaluate(_signal(), ctx)
    assert "trading_paused" in decision.reasons
    engine.resume_trading()

    engine.engage_kill_switch("manual_review")
    ctx = RiskContext(account=_account())
    decision = engine.evaluate(_signal(), ctx)
    assert "emergency_kill_switch" in decision.reasons

    snapshot = engine.snapshot()
    assert snapshot["kill_switch"] is True
    assert snapshot["halt_reasons"] == ["manual_review"]
    assert snapshot["paused"] is False
    assert snapshot["stats"]["rejected"] >= 1


def test_risk_engine_kill_switch_audit_events() -> None:
    audit_log.clear()
    engine, _ = _engine()
    engine.engage_kill_switch("test")
    engine.release_kill_switch()
    engine.pause_trading("test")
    engine.resume_trading()
    types = {e.event_type for e in audit_log.recent(20)}
    assert "EMERGENCY_STOP" in types
    assert "RISK_KILL_SWITCH_RELEASED" in types
    assert "RISK_TRADING_PAUSED" in types
    assert "RISK_TRADING_RESUMED" in types


def test_risk_engine_evaluate_order() -> None:
    engine, _ = _engine()
    order = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        stop_loss=2490.0,
        take_profit=2520.0,
        status=OrderStatus.CREATED,
    )
    ctx = RiskContext(account=_account())
    decision = engine.evaluate_order(order, ctx)
    assert decision.approved

    no_stop = OrderRequest(
        symbol="XAUUSD", side=OrderSide.BUY, volume=0.01, entry=2500.0
    )
    decision2 = engine.evaluate_order(
        no_stop, RiskContext(account=_account(), stop_loss_required=True)
    )
    assert not decision2.approved
    assert "stop_loss_required" in decision2.reasons


def test_risk_api_surface() -> None:
    clear_settings_cache()
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="memory",
        max_position_size=0.10,
        max_risk_per_trade_pct=1.0,
    )
    app = create_app(settings)

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = (await client.get("/api/v1/status")).json()
            assert status["kill_switch"] is False
            assert status["trading_paused"] is False
            assert status["trading_allowed"] is True

            body = (await client.get("/api/v1/risk/status")).json()
            assert body["kill_switch"] is False
            assert body["settings"]["max_position_size"] == 0.10
            assert body["stats"]["checks"] == 0

            resp = await client.post("/api/v1/risk/killswitch", json={"engaged": True, "reason": "test"})
            assert resp.status_code == 200
            assert resp.json()["kill_switch"] is True
            assert (await client.get("/api/v1/status")).json()["trading_allowed"] is False

            resp = await client.post("/api/v1/risk/pause", json={"paused": True})
            assert resp.json()["paused"] is True

            resp = await client.post("/api/v1/risk/killswitch", json={"engaged": False})
            assert resp.json()["kill_switch"] is False
            resp = await client.post("/api/v1/risk/pause", json={"paused": False})
            assert resp.json()["paused"] is False

            signal = _signal().model_dump(mode="json")
            account = _account().model_dump(mode="json")
            resp = await client.post(
                "/api/v1/risk/evaluate",
                json={
                    "signal": signal,
                    "account": account,
                    "context": {"proposed_volume": 0.01},
                },
            )
            assert resp.status_code == 200
            assert resp.json()["approved"] is True

            bad_stop = _signal(stop_loss=None).model_dump(mode="json")
            resp = await client.post(
                "/api/v1/risk/evaluate",
                json={"signal": bad_stop, "account": account},
            )
            assert resp.json()["approved"] is False
            assert "stop_loss_required" in resp.json()["reasons"]
    asyncio.run(run())


def test_risk_evaluate_requires_valid_context_values() -> None:
    from mt5_platform.api import RiskEvaluateRequest

    payload = {
        "signal": _signal().model_dump(mode="json"),
        "account": _account().model_dump(mode="json"),
        "context": {"open_positions": 2},
    }
    req = RiskEvaluateRequest.model_validate(payload)
    assert req.context.open_positions == 2
    with pytest.raises(ValueError):
        RiskEvaluateRequest.model_validate(
            {
                "signal": _signal().model_dump(mode="json"),
                "account": _account().model_dump(mode="json"),
                "context": {"proposed_volume": 0.0},
            }
        )


def test_risk_engine_stats_approval_rate() -> None:
    engine, _ = _engine()
    ctx_ok = RiskContext(account=_account(), proposed_volume=0.01)
    engine.evaluate(_signal(), ctx_ok)
    engine.evaluate(_signal(stop_loss=None), RiskContext(account=_account()))
    assert engine._stats.checks == 2  # noqa: SLF001
    assert engine._stats.approved == 1
    assert engine._stats.rejected == 1
    assert engine._stats.reject_reasons["stop_loss_required"] == 1
    assert engine.snapshot()["stats"]["approval_rate"] == 0.5
    assert len(engine.snapshot()["recent_decisions"]) == 2