"""FastAPI application factory and HTTP surface."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, ComponentHealth, Severity
from mt5_platform.common.events import AccountSnapshot, AuditEvent, MarketDataEvent, StrategySignal
from mt5_platform.config import Settings, get_settings
from mt5_platform.execution import build_execution_adapter
from mt5_platform.observability import build_health_payload, configure_logging
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.signals import build_signal_engine
from mt5_platform.storage import create_store_from_settings
from mt5_platform.storage.db import init_db
from mt5_platform.strategy import describe_available


class RiskKillSwitchRequest(BaseModel):
    engaged: bool
    reason: str = ""


class RiskPauseRequest(BaseModel):
    paused: bool
    reason: str = ""


class RiskContextRequest(BaseModel):
    open_positions: int = 0
    current_spread: float | None = None
    data_age_ms: float | None = None
    duplicate_position: bool = False
    market_session_ok: bool = True
    stop_loss_required: bool = True
    proposed_volume: float | None = Field(default=None, gt=0)
    current_exposure: float | None = Field(default=None, ge=0)
    current_slippage: float | None = Field(default=None, ge=0)


class RiskEvaluateRequest(BaseModel):
    signal: StrategySignal
    account: AccountSnapshot
    context: RiskContextRequest = Field(default_factory=RiskContextRequest)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    store = create_store_from_settings(settings)
    signal_engine = build_signal_engine(settings, store)
    risk_engine = RiskEngine(settings=settings)
    order_manager = OrderManager(
        settings=settings, store=store, risk_engine=risk_engine
    )
    execution_adapter = build_execution_adapter(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = getattr(store, "_engine", None)
        if engine is not None:
            await init_db(engine)
        app.state.store = store
        audit_log.emit(
            AuditEvent(
                component="api",
                event_type=AuditEventType.SYSTEM_START.value,
                severity=Severity.INFO,
                payload={
                    "trading_mode": settings.trading_mode.value,
                    "is_live": settings.is_live,
                    "storage_backend": settings.storage_backend,
                },
            )
        )
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Modular MT5 trading platform API (demo-first)",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.signal_engine = signal_engine
    app.state.risk_engine = risk_engine
    app.state.order_manager = order_manager
    app.state.execution_adapter = execution_adapter

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def _storage_health() -> ComponentHealth:
        if settings.storage_backend == "memory":
            return ComponentHealth.UP
        checker = getattr(store, "healthcheck", None)
        if checker is None:
            return ComponentHealth.DISABLED
        try:
            ok = await checker()
            return ComponentHealth.UP if ok else ComponentHealth.DOWN
        except Exception:
            return ComponentHealth.DOWN

    @app.get("/health")
    async def health() -> dict:
        storage = await _storage_health()
        return build_health_payload(
            settings,
            components={
                "api": ComponentHealth.UP,
                "ingestion": (
                    ComponentHealth.DISABLED
                    if not settings.ingestion_enabled
                    else ComponentHealth.DOWN
                ),
                "storage": storage,
                "strategy": ComponentHealth.UP,
                "risk": ComponentHealth.UP,
                "execution": (
                    ComponentHealth.UP
                    if settings.execution_backend == "mock"
                    else ComponentHealth.DISABLED
                ),
                "mt5": ComponentHealth.DISABLED,
            },
        )

    @app.get("/api/v1/status")
    async def status() -> dict:
        return {
            "trading_mode": settings.trading_mode.value,
            "is_demo": settings.is_demo,
            "is_live": settings.is_live,
            "kill_switch": risk_engine.kill_switch,
            "trading_paused": risk_engine.paused,
            "trading_allowed": (
                settings.trading_allowed and not risk_engine.kill_switch and not risk_engine.paused
            ),
            "default_symbol": settings.default_symbol,
            "ingestion_enabled": settings.ingestion_enabled,
            "storage_backend": settings.storage_backend,
            "strategies": settings.active_strategies,
            "execution_backend": settings.execution_backend,
            "phase": 6,
        }

    @app.get("/api/v1/orders")
    async def list_orders(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        orders = order_manager.recent_orders(limit=limit)
        return {
            "orders": [o.model_dump(mode="json") for o in orders],
            "stats": order_manager.stats_snapshot(),
        }

    @app.get("/api/v1/orders/stats")
    async def order_stats() -> dict:
        return order_manager.stats_snapshot()

    @app.get("/api/v1/orders/{order_id}")
    async def order_detail(order_id: str) -> dict:
        order = order_manager.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return {
            "order": order.model_dump(mode="json"),
            "history": order_manager.history(order_id),
        }

    @app.get("/api/v1/executions")
    async def list_executions(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        records = execution_adapter.executions[-limit:][::-1]
        return {"executions": [r.model_dump(mode="json") for r in records]}

    @app.get("/api/v1/audit/recent")
    async def recent_audit(limit: int = 50) -> dict:
        events = audit_log.recent(limit=min(limit, 500))
        return {"events": [e.model_dump(mode="json") for e in events]}

    @app.get("/api/v1/market/ticks")
    async def list_ticks(
        symbol: str | None = None,
        limit: int = Query(default=100, ge=1, le=10_000),
    ) -> dict:
        getter = getattr(store, "get_ticks", None)
        if getter is None:
            return {"ticks": []}
        ticks = await getter(symbol=symbol, limit=limit)
        return {"ticks": [t.model_dump(mode="json") for t in ticks]}

    @app.get("/api/v1/market/candles")
    async def list_candles(
        symbol: str = Query(...),
        timeframe: str = Query(default="1m"),
        limit: int = Query(default=100, ge=1, le=5000),
    ) -> dict:
        getter = getattr(store, "get_candles", None)
        if getter is None:
            return {"candles": []}
        candles = await getter(symbol=symbol, timeframe=timeframe, limit=limit)
        return {
            "candles": [
                {
                    "timestamp": c.timestamp.isoformat(),
                    "symbol": c.symbol,
                    "timeframe": c.timeframe,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                    "tick_count": c.tick_count,
                    "spread_avg": c.spread_avg,
                }
                for c in candles
            ]
        }

    @app.get("/api/v1/strategies")
    async def list_strategies() -> dict:
        return {"strategies": signal_engine.list_strategies()}

    @app.get("/api/v1/strategies/available")
    async def list_available_strategies() -> dict:
        return {"available": describe_available()}

    @app.post("/api/v1/strategies/{name}/enable")
    async def enable_strategy(name: str) -> dict:
        strategy = await signal_engine.enable(name.strip().lower())
        if strategy is None:
            raise HTTPException(status_code=404, detail=f"unknown strategy: {name}")
        return {"name": strategy.name, "enabled": strategy.enabled}

    @app.post("/api/v1/strategies/{name}/disable")
    async def disable_strategy(name: str) -> dict:
        strategy = await signal_engine.disable(name.strip().lower())
        if strategy is None:
            raise HTTPException(status_code=404, detail=f"unknown strategy: {name}")
        return {"name": strategy.name, "enabled": strategy.enabled}

    @app.get("/api/v1/signals")
    async def list_signals(
        symbol: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        getter = getattr(store, "get_signals", None)
        if getter is not None:
            signals = await getter(symbol=symbol, limit=limit)
        else:
            signals = signal_engine.recent_signals(limit=limit)
        return {"signals": [s.model_dump(mode="json") for s in signals]}

    @app.get("/api/v1/signals/stats")
    async def signal_stats() -> dict:
        return signal_engine.stats_snapshot()

    @app.post("/api/v1/signals/evaluate")
    async def evaluate_market_data(event: MarketDataEvent) -> dict:
        emitted = await signal_engine.on_market_data(event)
        return {
            "events_processed": signal_engine.stats.events_processed,
            "signals_generated": signal_engine.stats.signals_generated,
            "signals": [s.model_dump(mode="json") for s in emitted],
        }

    @app.get("/api/v1/risk/status")
    async def risk_status() -> dict:
        return risk_engine.snapshot()

    @app.post("/api/v1/risk/killswitch")
    async def set_kill_switch(req: RiskKillSwitchRequest) -> dict:
        if req.engaged:
            risk_engine.engage_kill_switch(req.reason or "manual")
        else:
            risk_engine.release_kill_switch()
        return {
            "kill_switch": risk_engine.kill_switch,
            "halt_reasons": risk_engine.halt_reasons,
        }

    @app.post("/api/v1/risk/pause")
    async def set_pause(req: RiskPauseRequest) -> dict:
        if req.paused:
            risk_engine.pause_trading(req.reason or "manual")
        else:
            risk_engine.resume_trading()
        return {
            "paused": risk_engine.paused,
            "pause_reasons": risk_engine.pause_reasons,
        }

    @app.post("/api/v1/risk/evaluate")
    async def evaluate_risk(req: RiskEvaluateRequest) -> dict:
        ctx = RiskContext(account=req.account, **req.context.model_dump())
        decision = risk_engine.evaluate(req.signal, ctx)
        return {
            "approved": decision.approved,
            "reasons": decision.reasons,
            "checked_at": decision.checked_at.isoformat(),
            "correlation_id": decision.correlation_id,
        }

    return app
