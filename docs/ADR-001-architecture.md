"""Architecture decision record — Phase 1."""

# ADR-001: Modular pipeline with hard separation of ingestion and execution

## Context

The platform must collect authorized market/dashboard data, run strategies, enforce risk,
and execute via MT5 — without allowing scrapers to place trades, and without enabling
live trading until validation is complete.

## Decision

1. **Python + FastAPI** for the control plane and trading services (MetaTrader5 is Python-native;
   asyncio fits Playwright ingestion).
2. **Package-per-concern** under `src/mt5_platform/` matching the pipeline stages.
3. **Pydantic contracts** (`MarketDataEvent`, `StrategySignal`, `OrderRequest`, `ExecutionRecord`)
   as the only allowed cross-module payloads.
4. **Demo by default**; live mode dual-gated in `Settings`.
5. **MockExecutionAdapter** for all tests until Phase 7 demo adapter.
6. **In-memory stores** in Phase 1; TimescaleDB/Redis wired in Phase 3.
7. Ingestion workers are **event producers only** — no imports of order submission APIs
   in scraper call paths (enforced by review + future lint boundaries).

## Consequences

- Incremental delivery by phase is mandatory.
- Frontend (Phase 8) consumes `/health` and `/api/v1/*` only.
- Profitability claims require measured backtest/forward-test artifacts (Phase 9+).
