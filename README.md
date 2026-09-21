# MT5 Automated Trading Platform

Production-oriented, modular MetaTrader 5 trading platform with **demo-first** execution,
mandatory risk checks, and a web control plane.

> **Status:** Stage A complete — MT5 execution adapter, continuous trading runtime with
> API-owned lifecycle control (`BotControlService`), React control-room dashboard,
> backtest engine with go/no-go gates, and a hard live-mode validation gate.
> Intelligence layer (context/agents/synthesis/historical/position/learning) is wired
> behind `INTELLIGENCE_ENABLED` (default off). No profitability claims. No live trading
> until the validation gates pass on real data — see "Backtest -> demo -> live".

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
  common/        # events, enums, audit, IDs, money-correct instruments
  config.py      # settings + live-mode gates
  ingestion/     # M1 browser pool, M2 proxies, M3 resource policy
  pipeline/      # validation / stale / duplicate gates
  storage/       # memory | sqlite | postgres stores
  context/       # Market Context Engine (canonical MarketContext, regime)
  agents/        # multi-agent intelligence: analytical + support + synthesis
  historical/    # Historical Evidence Engine (similarity, expectancy, MAE/MFE)
  learning/      # DecisionMemory, PostTradeReview, HypothesisRegistry
  position/      # Position Intelligence: HOLD/MODIFY/REDUCE/EXIT/EMERGENCY_EXIT
  strategy/      # Strategy ABC + sma_crossover/breakout/mean_reversion/momentum
  risk/          # mandatory RiskEngine + persisted kill switch
  orders/        # order state machine (broker state is truth)
  execution/     # MockExecutionAdapter + real MT5ExecutionAdapter
  runtime/       # MT5CandleFeed + TradingLoop + BotControlService + intelligence wiring
  account/       # account monitoring
  observability/ # health payloads
  api/           # FastAPI routes (incl. runtime control + account/positions/quote)
  backtest/      # engine, metrics, gates, sweeps, Dukascopy fetch, validation gate
  main.py        # uvicorn entry
frontend/        # React + Vite control-room dashboard
deploy/          # Windows EC2 setup, Task Scheduler autostart, Caddy config
```

## Development phases

1. Skeleton / architecture
2. Data ingestion + normalization
3. Storage + historical data
4. Strategy / signal engine
5. Risk engine
6. Mock execution engine
7. **Market Context Engine + regime classification (Phase A of the intelligence layer)** ← current
8. Multi-agent system + debate/synthesis + trade thesis
9. Historical evidence engine
10. Position intelligence
11. Learning / decision memory
12. Orchestrator (continuous agent cycle)
13. MT5 **demo** adapter
14. Web dashboard (control room)
15. Backtests + forward validation
16. Security / reliability audit
17. Explicitly gated **live** mode (only after validation)

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

## Backtest -> demo -> live (in this order)

```bash
# 1. get data (pick one)
python -m mt5_platform.backtest fetch-mt5        --symbol XAUUSD --timeframe M15 --days 365   # Windows + MT5, exact broker prices
python -m mt5_platform.backtest fetch-dukascopy  --symbol XAUUSD --timeframe M15 --days 365   # free, any OS (try --days 3 first)
# 2. test one strategy: chronological in/out-of-sample split, real costs, go/no-go gates
python -m mt5_platform.backtest run   --csv data/xauusd_m15.csv --strategy sma_crossover --balance 1000
# 3. optional sweep (ranked in-sample, verified out-of-sample, overfitting flagged)
python -m mt5_platform.backtest sweep --csv data/xauusd_m15.csv --grid fast_period=3,5,8 --grid slow_period=20,30,50
# 4. see what YOUR account can actually trade (needs MT5 running, demo login in .env)
python -m mt5_platform.runtime check --symbol XAUUSD
# 5. run the bot on demo
python -m mt5_platform.runtime run --symbol XAUUSD --timeframe M15
```

The engine fills at the *next* bar's open, pays spread + slippage, assumes the stop hits first
when stop and target share a bar, and skips trades the live bot would refuse for being too big
for the account. `run` writes `validation_report.json`; LIVE mode will not start without a
passing report for the same symbol and timeframe. Passing the gates means "not obviously
broken", never "profitable" — forward-test on demo before real money.

## Control-room dashboard

A React + Vite dashboard lives in `frontend/` and consumes the authenticated FastAPI control plane.

```bash
cd frontend
npm install
copy .env.example .env.local
npm run dev
```

Set `VITE_API_BASE_URL` to the deployed API URL. The dashboard stores the bearer token only in
session storage and uses the runtime API to start/stop/restart the MT5 worker.

## MT5 demo adapter

`EXECUTION_BACKEND=mt5` uses the real `MetaTrader5` package, so it must run on **Windows**
with the MT5 terminal installed (a Windows VPS is the usual setup). The dashboard/website can
live anywhere and talk to this API over HTTPS with `API_TOKEN` set.

- Refuses to connect to a real-money account unless both live flags are set.
- Every order needs a stop loss; SL/TP are sent with the order so the broker holds them.
- Orders are tagged (magic + comment) — a retry or crash can never open a duplicate, and
  `reconcile` recovers uncertain outcomes from broker truth.
- Risk/exposure use the broker's contract spec (`common/instruments.py`); with the mt5
  backend a missing spec rejects the trade. Use `position_size_for_risk()` to size trades —
  on a small account it correctly returns `0.0` (no trade) when even the minimum lot is too big.
- Kill switch and pause persist across restarts when `RISK_STATE_PATH` is set.

## Safety

- Default `TRADING_MODE=demo`
- Live mode refuses to boot without dual acknowledgment flags
- Mock adapter for tests; the `mt5` backend refuses real-money accounts until live is acknowledged
- Signals never become orders without a risk approval; kill switch blocks submission
- Risk math uses the executable price (`execution_entry` from the live quote), not the stale signal entry
- Broker state is truth: reconciliation corrects local order state and audits every mismatch
- Do not bypass risk or order-management layers from ingestion workers
- Windows deployment: see `docs/RUNBOOK-AWS.md` (one-machine setup, Task Scheduler autostart, HTTPS)
