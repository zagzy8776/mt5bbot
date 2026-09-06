# MT5 Automated Trading Platform

Production-oriented, modular MetaTrader 5 trading platform with **demo-first** execution,
mandatory risk checks, and a web control plane.

> **Status:** Phase 5 — risk engine (full mandatory-check coverage, kill switch, pause, stats).
> No profitability claims. No live trading until Phase 11 validation gates pass.

## Architecture

```
DATA INGESTION  →  NORMALIZATION / VALIDATION  →  TIME-SERIES STORAGE
        ↓
  SIGNAL ENGINE  →  STRATEGY ENGINE  →  RISK ENGINE  →  ORDER MANAGER
        ↓
  MT5 EXECUTION ADAPTER  →  TRADE / ACCOUNT STATE
        ↓
  WEB DASHBOARD  +  AUDIT LOG
```

**Hard rules**

- Scrapers / data sources emit `MarketDataEvent` only — they never place trades.
- `StrategySignal` ≠ order. Signals must pass `RiskEngine` before `OrderManager`.
- Execution adapters only submit risk-approved orders and reconcile broker state.
- `TradingMode.LIVE` requires `LIVE_TRADING_ENABLED=true` **and** `LIVE_TRADING_ACKNOWLEDGED=true`.
- Emergency kill switch blocks all new orders.

## Stack (Phase 1)

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| API | FastAPI |
| Schemas | Pydantic v2 |
| Logging | structlog (JSON) |
| Ingestion (Phase 2) | Playwright (optional extra) |
| Broker (Phase 7) | MetaTrader5 package (optional extra) |
| Storage (Phase 3) | PostgreSQL + TimescaleDB / Redis |
| Dashboard (Phase 8) | Vite + React (scaffold later) |

## Package layout

```
src/mt5_platform/
  common/        # events, enums, audit, IDs
  config.py      # settings + live-mode gates
  ingestion/     # M1 browser pool, M2 proxies, M3 resource policy
  pipeline/      # validation / stale / duplicate gates
  storage/       # store interfaces + in-memory Phase 1
  strategy/      # Strategy ABC (signals only)
  risk/          # mandatory RiskEngine + kill switch
  orders/        # order state machine
  execution/     # MockExecutionAdapter + MT5 stub
  account/       # account monitoring
  observability/ # health payloads
  api/           # FastAPI routes
  main.py        # uvicorn entry
```

## Development phases

1. Skeleton / architecture
2. Data ingestion + normalization
3. Storage + historical data
4. Strategy / signal engine
5. Risk engine
6. **Mock execution engine (OrderManager + state machine + reconciliation)** ← current
7. MT5 **demo** adapter
8. Web dashboard
9. Backtests + forward tests
10. Security / reliability audit
11. Explicitly gated **live** mode (only after validation)

## Quick start

```bash
cd "e:\mt5 bot"
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
pytest
uvicorn mt5_platform.main:run --factory  # or: python -m mt5_platform.main
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Running the API

```bash
pip install -e ".[dev]"
python -m mt5_platform.main
```

Ingestion dry-run (synthetic quotes only — never places trades):

```bash
python -m mt5_platform.ingestion --workers 4 --max-events 20
```

Optional Playwright extra (real Chromium; authorize dashboards before use):

```bash
pip install -e ".[ingestion]"
playwright install chromium
```

Persistent storage (SQLite local / Postgres+Timescale via Docker):

```bash
pip install -e ".[storage]"
# .env: STORAGE_BACKEND=sqlite  or  STORAGE_BACKEND=postgres
docker compose up -d   # optional Timescale + Redis
```

- `GET /health` — component health + trading mode
- `GET /api/v1/status` — demo/live flags, kill switch, phase, storage backend, active strategies
- `GET /api/v1/audit/recent` — recent audit events
- `GET /api/v1/market/ticks` — historical ticks
- `GET /api/v1/market/candles?symbol=XAUUSD&timeframe=1m` — OHLC candles
- `GET /api/v1/strategies` — registered strategies + per-strategy stats
- `GET /api/v1/strategies/available` — strategy factory catalog (descriptions + params)
- `POST /api/v1/strategies/{name}/enable` | `POST /api/v1/strategies/{name}/disable` — runtime control
- `GET /api/v1/signals` — historical signals (store-backed)
- `GET /api/v1/signals/stats` — signal-engine statistics
- `POST /api/v1/signals/evaluate` — feed one market-data tick to the signal engine
- `GET /api/v1/risk/status` — kill switch, pause state, risk limits, stats, recent decisions
- `POST /api/v1/risk/killswitch` — engage/release the emergency kill switch
- `POST /api/v1/risk/pause` — pause/resume new-trade evaluation
- `POST /api/v1/risk/evaluate` — full risk gate for a proposed signal + account context
- `GET /api/v1/orders` — recent orders + lifecycle stats
- `GET /api/v1/orders/stats` — order manager statistics
- `GET /api/v1/orders/{order_id}` — order detail + full state-transition history
- `GET /api/v1/executions` — execution records (fill price, slippage, rejections)

Optional infra for later phases:

```bash
docker compose up -d
```

## Safety

- Default `TRADING_MODE=demo`
- Live mode refuses to boot without dual acknowledgment flags
- Mock adapter is the only executable execution backend (`mt5` stub raises until Phase 7)
- Signals never become orders without a risk approval; kill switch blocks submission
- Broker state is truth: reconciliation corrects local order state and audits every mismatch
- Do not bypass risk or order-management layers from ingestion workers
