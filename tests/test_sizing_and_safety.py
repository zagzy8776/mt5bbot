"""Instrument math, strict risk mode, persisted kill switch, and API auth."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import AccountSnapshot, StrategySignal
from mt5_platform.common.instruments import DEFAULT_SPECS, InstrumentSpec, position_size_for_risk
from mt5_platform.config import Settings
from mt5_platform.risk import RiskContext, RiskEngine

GOLD = DEFAULT_SPECS["XAUUSD"]


def _account(equity: float = 10_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        balance=equity,
        equity=equity,
        free_margin=equity * 0.95,
        used_margin=equity * 0.05,
        floating_pnl=0.0,
    )


def _signal(entry=2500.0, stop=2495.0) -> StrategySignal:
    return StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=entry,
        stop_loss=stop,
        take_profit=entry + 10,
        confidence=0.8,
    )


# ------------------------------------------------------------------ instrument math


def test_gold_money_math() -> None:
    assert GOLD.value_per_price_unit == pytest.approx(100.0)  # $100 per $1 move per lot
    assert GOLD.risk_money(2500.0, 2495.0, 0.10) == pytest.approx(50.0)
    assert GOLD.notional(2500.0, 0.01) == pytest.approx(2500.0)


def test_volume_rules() -> None:
    assert GOLD.volume_is_valid(0.05) == []
    assert GOLD.volume_is_valid(0.015) == ["volume_not_on_step"]
    assert "volume_below_min" in GOLD.volume_is_valid(0.001)
    assert "volume_above_max" in GOLD.volume_is_valid(1000.0)
    assert GOLD.normalize_volume(0.037) == pytest.approx(0.03)  # always down
    assert GOLD.normalize_volume(0.004) == 0.0


def test_bad_spec_is_rejected() -> None:
    with pytest.raises(ValueError):
        InstrumentSpec("X", contract_size=0.0, tick_size=0.01, tick_value=1.0)


def test_size_for_risk_scales_with_equity() -> None:
    # $10,000 * 1% = $100 budget; $5 stop = $500/lot -> 0.20 lot
    assert position_size_for_risk(
        equity=10_000.0, risk_pct=1.0, entry=2500.0, stop=2495.0, spec=GOLD
    ) == pytest.approx(0.20)


def test_size_for_risk_rounds_down_never_up() -> None:
    # $1,000 * 1% = $10 budget; $5 stop = $500/lot -> 0.02 lot exactly
    assert position_size_for_risk(
        equity=1_000.0, risk_pct=1.0, entry=2500.0, stop=2495.0, spec=GOLD
    ) == pytest.approx(0.02)
    # $1,900 -> 0.038 -> 0.03
    assert position_size_for_risk(
        equity=1_900.0, risk_pct=1.0, entry=2500.0, stop=2495.0, spec=GOLD
    ) == pytest.approx(0.03)


def test_small_account_correctly_gets_no_trade() -> None:
    """A $100 account cannot trade gold with a $4 stop at 1% risk: even 0.01 lot risks $4."""
    size = position_size_for_risk(equity=100.0, risk_pct=1.0, entry=2500.0, stop=2496.0, spec=GOLD)
    assert size == 0.0


def test_size_for_risk_respects_max_volume_and_garbage_input() -> None:
    assert position_size_for_risk(
        equity=1e6, risk_pct=1.0, entry=2500.0, stop=2495.0, spec=GOLD, max_volume=0.5
    ) == pytest.approx(0.5)
    for bad in (float("nan"), 0.0, -5.0):
        assert (
            position_size_for_risk(equity=bad, risk_pct=1.0, entry=2500.0, stop=2495.0, spec=GOLD)
            == 0.0
        )
    assert (
        position_size_for_risk(equity=1000.0, risk_pct=1.0, entry=2500.0, stop=2500.0, spec=GOLD)
        == 0.0
    )


# ----------------------------------------------------------------------- risk engine


def test_old_price_unit_math_missed_the_100x_gold_error() -> None:
    engine = RiskEngine(settings=Settings(max_position_size=0.10))
    ctx_legacy = RiskContext(account=_account(1_000.0), proposed_volume=0.10)
    assert engine.evaluate(_signal(), ctx_legacy).approved  # legacy: $0.50 "risk"
    ctx_money = RiskContext(account=_account(1_000.0), proposed_volume=0.10, instrument=GOLD)
    decision = engine.evaluate(_signal(), ctx_money)  # real: $50 risk = 5%
    assert "max_risk_per_trade" in decision.reasons


def test_exposure_uses_contract_size() -> None:
    engine = RiskEngine(settings=Settings(max_position_size=1.0, max_exposure_pct=500.0))
    # 0.10 lot * 100 oz * 2500 = $25,000 notional on $1,000 equity = 2500%
    ctx = RiskContext(account=_account(1_000.0), proposed_volume=0.10, instrument=GOLD)
    assert "max_exposure" in engine.evaluate(_signal(stop=2499.9), ctx).reasons


def test_broker_volume_rules_enforced_by_risk_engine() -> None:
    engine = RiskEngine(settings=Settings(max_position_size=1.0))
    ctx = RiskContext(account=_account(), proposed_volume=0.015, instrument=GOLD)
    assert "volume_not_on_step" in engine.evaluate(_signal(), ctx).reasons


def test_real_execution_fails_closed_without_instrument_spec() -> None:
    engine = RiskEngine(settings=Settings(execution_backend="mt5", max_position_size=1.0))
    decision = engine.evaluate(_signal(), RiskContext(account=_account(), proposed_volume=0.01))
    assert not decision.approved and "instrument_spec_missing" in decision.reasons


def test_good_trade_still_approved_with_spec() -> None:
    engine = RiskEngine(settings=Settings(execution_backend="mt5", max_position_size=1.0))
    ctx = RiskContext(account=_account(), proposed_volume=0.10, instrument=GOLD)
    decision = engine.evaluate(_signal(), ctx)  # $50 risk on $10,000 = 0.5%
    assert decision.approved, decision.reasons


# ----------------------------------------------------------------- kill-switch memory


def test_kill_switch_survives_restart(tmp_path) -> None:
    state = tmp_path / "risk_state.json"
    settings = Settings(risk_state_path=str(state))
    RiskEngine(settings=settings).engage_kill_switch("max_daily_loss")

    after_restart = RiskEngine(settings=settings)
    assert after_restart.kill_switch is True
    assert "max_daily_loss" in after_restart.halt_reasons
    ctx = RiskContext(account=_account())
    assert "emergency_kill_switch" in after_restart.evaluate(_signal(), ctx).reasons

    after_restart.release_kill_switch()
    assert RiskEngine(settings=settings).kill_switch is False


def test_pause_survives_restart(tmp_path) -> None:
    settings = Settings(risk_state_path=str(tmp_path / "s.json"))
    RiskEngine(settings=settings).pause_trading("lunch")
    assert RiskEngine(settings=settings).paused is True


def test_corrupt_state_file_fails_closed(tmp_path) -> None:
    state = tmp_path / "s.json"
    state.write_text("{not json", encoding="utf-8")
    engine = RiskEngine(settings=Settings(risk_state_path=str(state)))
    assert engine.kill_switch is True


def test_state_file_is_valid_json(tmp_path) -> None:
    state = tmp_path / "s.json"
    RiskEngine(settings=Settings(risk_state_path=str(state))).engage_kill_switch("x")
    assert json.loads(state.read_text())["kill_switch"] is True


# ------------------------------------------------------------------------- API auth


def _client(settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


async def test_api_requires_bearer_token_when_configured() -> None:
    async with _client(Settings(api_token="s3cret", storage_backend="memory")) as c:
        assert (await c.get("/health")).status_code == 200  # liveness stays public
        assert (await c.get("/api/v1/status")).status_code == 401
        kill = await c.post("/api/v1/risk/killswitch", json={"engaged": False})
        assert kill.status_code == 401
        bad = await c.get("/api/v1/status", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        ok = await c.get("/api/v1/status", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


async def test_api_open_on_loopback_without_token() -> None:
    async with _client(Settings(storage_backend="memory")) as c:
        assert (await c.get("/api/v1/status")).status_code == 200


def test_public_host_without_token_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="API_TOKEN"):
        create_app(Settings(api_host="0.0.0.0", storage_backend="memory"))
    create_app(Settings(api_host="0.0.0.0", api_token="x", storage_backend="memory"))
