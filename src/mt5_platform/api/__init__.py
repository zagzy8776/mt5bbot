"""FastAPI application factory and HTTP surface."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, ComponentHealth, Severity
from mt5_platform.common.events import AccountSnapshot, AuditEvent, MarketDataEvent, StrategySignal
from mt5_platform.config import Settings, get_settings
from mt5_platform.execution import build_execution_adapter
from mt5_platform.observability import build_health_payload, configure_logging
from mt5_platform.orders import OrderManager
from mt5_platform.research.report import build_research_status
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.runtime import BotControlService
from mt5_platform.runtime.heartbeat import build_runtime_heartbeat
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
    execution_entry: float | None = Field(default=None, gt=0)


class RuntimeControlRequest(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None


class RiskEvaluateRequest(BaseModel):
    signal: StrategySignal
    account: AccountSnapshot
    context: RiskContextRequest = Field(default_factory=RiskContextRequest)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    if settings.api_host not in _LOOPBACK_HOSTS and not settings.api_token:
        raise ValueError(
            "API_TOKEN is required when API_HOST is not loopback: this API can release the "
            "kill switch and enable strategies. Refusing to start unauthenticated."
        )
    store = create_store_from_settings(settings)
    signal_engine = build_signal_engine(settings, store)
    risk_engine = RiskEngine(settings=settings)
    order_manager = OrderManager(settings=settings, store=store, risk_engine=risk_engine)
    execution_adapter = build_execution_adapter(settings)
    runtime_service = BotControlService(
        settings=settings,
        adapter=execution_adapter,
        signal_engine=signal_engine,
        risk_engine=risk_engine,
        order_manager=order_manager,
        store=store,
    )

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
        await runtime_service.stop()
        audit_log.emit(
            AuditEvent(
                component="api",
                event_type=AuditEventType.SYSTEM_STOP.value,
                severity=Severity.INFO,
                payload={"runtime_state": runtime_service.snapshot.get("state")},
            )
        )
        if engine is not None:
            await engine.dispose()

    async def require_auth(request: Request) -> None:
        """Bearer-token gate for every route except /health (unset token = local dev only)."""
        if not settings.api_token or request.url.path == "/health":
            return
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip().encode(), settings.api_token.encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Modular MT5 trading platform API (demo-first)",
        lifespan=lifespan,
        dependencies=[Depends(require_auth)],
    )
    app.state.settings = settings
    app.state.store = store
    app.state.signal_engine = signal_engine
    app.state.risk_engine = risk_engine
    app.state.order_manager = order_manager
    app.state.execution_adapter = execution_adapter
    app.state.runtime_service = runtime_service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
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
                "mt5": (
                    ComponentHealth.UP
                    if runtime_service.snapshot["connected"]
                    else ComponentHealth.DEGRADED
                    if settings.execution_backend == "mt5"
                    else ComponentHealth.DISABLED
                ),
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
            "runtime": runtime_service.snapshot,
        }

    @app.get("/api/v1/runtime")
    async def runtime_status() -> dict:
        return runtime_service.snapshot

    @app.get("/api/v1/runtime/stats")
    async def runtime_stats() -> dict:
        return runtime_service.snapshot.get("stats", {})

    @app.get("/api/v1/runtime/heartbeat")
    async def runtime_heartbeat() -> dict:
        """Read-only heartbeat: is the loop turning, and is the candle data fresh?

        Observational only — it reads the in-memory snapshot plus the risk engine's counters and
        computes freshness from the clock. It starts nothing, sizes nothing and changes nothing.
        """
        return build_runtime_heartbeat(
            runtime_service.snapshot,
            risk_snapshot=risk_engine.snapshot(),
        )

    @app.get("/api/v1/outcomes")
    async def outcomes(limit: int = 100, status: str | None = None) -> dict:
        """Recorded trade outcomes plus learning and evidence status.

        Populations stay separate: autonomous (this bot), external/manual and backtest are never
        mixed into the same statistics.
        """
        return await runtime_service.outcomes_payload(limit=limit, status=status)

    @app.post("/api/v1/runtime/start")
    async def runtime_start(req: RuntimeControlRequest | None = None) -> dict:
        req = req or RuntimeControlRequest()
        try:
            runtime_service.configure(symbol=req.symbol, timeframe=req.timeframe)
            return await runtime_service.start()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/runtime/stop")
    async def runtime_stop() -> dict:
        return await runtime_service.stop()

    @app.post("/api/v1/runtime/restart")
    async def runtime_restart() -> dict:
        try:
            return await runtime_service.restart()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/account")
    async def account_snapshot() -> dict:
        try:
            account = await runtime_service.refresh_account()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return account.model_dump(mode="json")

    @app.get("/api/v1/positions")
    async def positions_snapshot() -> dict:
        try:
            positions = await runtime_service.refresh_positions()
        except (RuntimeError, Exception) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "positions": [p.model_dump(mode="json") for p in positions],
            "manual_position_policy": settings.manual_position_policy.value,
        }

    @app.get("/api/v1/market/quote")
    async def market_quote(symbol: str = Query(..., min_length=1)) -> dict:
        adapter = execution_adapter
        if not getattr(adapter, "_connected", False) or not hasattr(adapter, "call"):
            raise HTTPException(status_code=503, detail="MT5 runtime is not connected")
        sym = symbol.strip()
        tick = await adapter.call("symbol_info_tick", sym)
        info = await adapter.call("symbol_info", sym)
        if tick is None or info is None or not float(tick.bid) or not float(tick.ask):
            raise HTTPException(status_code=404, detail=f"quote unavailable for {sym}")
        point = float(info.point) or 0.01
        return {
            "symbol": sym,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread_points": (float(tick.ask) - float(tick.bid)) / point,
            "time_msc": getattr(tick, "time_msc", None),
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

    @app.get("/api/v1/research/status")
    async def research_status() -> dict:
        """Research verdict, sealed-holdout state and forward evidence, in one place."""
        snapshot = runtime_service.snapshot
        attribution = (snapshot.get("stats") or {}).get("attribution") or {}
        return build_research_status(
            report_path=settings.research_report_path,
            manifest_path=settings.research_manifest_path,
            holdout_path=settings.research_holdout_path,
            runtime_state=str(snapshot.get("state", "") or ""),
            live_trading_enabled=settings.live_trading_enabled,
            trading_mode=settings.trading_mode.value,
            attribution=attribution,
        )

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
