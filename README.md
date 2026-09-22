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

## Manual / external positions

Broker positions that were not opened by this bot (different magic number) are never hidden.
`positions_get` returns them, they are tagged `is_external`, they count towards exposure and
duplicate/parallel-position limits, and every cycle records broker truth as lifecycle audit
events (`POSITION_DISCOVERED`, `POSITION_RECONCILED`, `POSITION_MONITORED`, `POSITION_HOLD`,
`POSITION_MODIFIED`, `POSITION_REDUCED`, `POSITION_EXIT_REQUESTED`, `POSITION_CLOSED`,
`POSITION_EXIT_REJECTED`) with ticket, policy, decision, reason and execution record.

`MANUAL_POSITION_POLICY` decides what may happen to them:

| value | discovered / audited | evaluated | may be modified or closed |
| --- | --- | --- | --- |
| `ignore` (default) | yes | no | no |
| `observe` | yes | yes | no — every action is refused and audited |
| `manage` | yes | yes | yes, **always** through RiskEngine → OrderManager |

When `manage` is on, a position action still has to pass the risk gate:

- Exits and reductions are never blocked by loss/drawdown/margin/exposure gates — those are the
  conditions that justify reducing risk. The kill switch stops *new* risk; it does not trap an
  open position.
- A modification may never increase risk: an existing stop can only move in the protective
  direction, and a new stop must sit on the correct side of the current price. Adding a first
  stop to an unprotected manual position is allowed because that lowers risk.
- A reduction must be a genuine partial volume for that specific position.

Visible in the dashboard: the Positions page shows the real MT5 ticket, a `bot`/`manual` badge,
the configured manual policy, and the position lifecycle event log.

## Trade outcomes and the historical ledger (Phase 1)

Nothing used to write `HistoricalOutcome`, so the bot recorded no completed trades and the
evidence engine was permanently empty. The live trade recorder now closes that loop:

    position opened -> record created with a frozen entry/setup snapshot -> MAE/MFE tracked
    -> partial exits recorded as legs -> final close -> HistoricalOutcome persisted
    -> post-trade review + decision memory -> evidence ledger

- **Passive by construction.** The recorder never blocks, delays or alters an order. Store
  failures are logged, queued and retried; the in-memory record survives and trading continues.
  A failed recorder can never change a strategy decision.
- **One record per broker position**, keyed by ticket with a deterministic trade id, so
  reconciliation seeing the same position twice cannot create a duplicate. Partial closes are
  legs of the same trade and only the final flat close freezes the outcome.
- **Nothing is invented.** Missing prices, commissions, swaps, profit and exit causes stay
  explicitly `None`/`unknown` with a `*_source` marker (`runtime`, `level_match`, `recovery`,
  `external`, `backtest`, `unavailable`). Exit causes are never guessed from P/L: the component
  that exits states the cause, or the exit price is matched against the recorded levels.
- **Survives restarts.** On startup the recorder reloads incomplete records (MAE/MFE preserved),
  reconciles against MT5, and finalizes trades that closed while the process was down, using the
  broker's closing deals for the real exit price and money when they exist.
- **Populations stay separate.** `autonomous` (this bot), `external`/manual and `backtest` are
  never mixed in the same statistics; the evidence ledger loads live populations only.
- **Learning proposes, it never applies.** Completed outcomes produce a review, a decision-memory
  record and *lesson hypotheses with no proposed change*. Promotion is still a deliberate step
  (research validation -> explicit configuration version), so no trade outcome can rewrite live
  configuration.
- **Same schema for research.** `backtest/outcomes.py` normalizes engine trades into the identical
  `HistoricalOutcome` model, so backtest and forward-demo outcomes can be compared on the same
  fields (MAE/MFE stay marked unavailable when the bar engine cannot provide them).

Storage: `outcomes` and `trade_legs` tables (indexed by symbol, strategy, timeframe, entry/exit
time, thesis id, ticket and status) are created with the existing schema initialization; the
complete model is also stored as JSON so a round trip is lossless. Aiven PostgreSQL remains the
production database.

Visible in the dashboard: the Outcomes & Learning card shows completed/recorded/pending outcomes,
MAE/MFE availability, the last completed trade and its exit cause, the autonomous vs manual vs
backtest split, and the evidence sample count against the unchanged thresholds
(weak 10 / moderate 30 / strong 100).

### Candle-shape features (Phase 2)

Every entry snapshot now also carries the shape of the closed candles the runtime actually had
(`candle_features`, versioned in `historical/features.py`): body/upper-wick/lower-wick ratios,
close position inside the range, range versus the window median, gap from the previous close,
consecutive same-direction candles, volume ratio against the window median, window high/low, plus
coarse rule-based labels (`marubozu`, `long_body`, `doji`, `hammer`, `shooting_star`, `normal`).
Values are computed from a bounded window (default 20 closed candles) with no lookahead; when
there is not enough history the field is explicitly `None`/absent rather than zero-filled, and the
feature version travels with the record so later changes cannot silently reinterpret old trades.

## In-trade management (Phase 3)

`INTELLIGENCE_ENABLED=true` puts the position manager in the loop: on every cycle the market
context is built from live ticks and each open position is evaluated (thesis validity, trailing
stop, break-even, reductions, exits). Each decision still travels
PositionManager → RiskEngine → OrderManager → broker, so a modification can only reduce risk and
an exit is never blocked by the loss/drawdown gates that justify it.

Two flags keep the concern separated:

| flag | effect |
| --- | --- |
| `INTELLIGENCE_ENABLED` | build context, evaluate positions, manage them dynamically |
| `INTELLIGENCE_ENTRIES_ENABLED` | additionally let the agent/synthesis path propose **new** entries (off by default, so the strategy registry stays the entry source of record) |

Exits performed by the manager are recorded as outcomes with the decision's own cause
(`thesis_invalidation`, `opposite_signal`, `trailing_stop`, `break_even`, `volatility_exit`,
`emergency_exit`, …) and the money/price come from the broker's closing deals. The dashboard's
In-trade management card shows contexts built, position evaluations, exits executed, the entry
gate state, the last decision (outcome, action, ticket, reason) and the decision counters.

## Macro / news calendar (Phase 4)

No calendar source is bundled, and nothing is invented: ingestion runs only once a provider is
configured (`NEWS_ENABLED=true` and `NEWS_PROVIDER=file|http`). Events keep their provenance
(provider, source, fetch time) and a stable `dedup_key`, so re-fetching a calendar cannot duplicate
rows. The MT5 Python build in use exposes no calendar API (verified at runtime), so the terminal
cannot be the source here.

```bash
# a local file the operator maintains (data/news_events.json: [{"date": "...Z", "title": "...",
# "currency": "USD", "impact": "high"}])
NEWS_ENABLED=true
NEWS_PROVIDER=file
NEWS_FILE_PATH=./data/news_events.json

# or an allow-listed JSON endpoint (empty allow-list = no request is ever made)
NEWS_PROVIDER=http
NEWS_HTTP_URL=https://example.org/calendar.json
NEWS_HTTP_ALLOW=https://example.org/
```

`NEWS_BLACKOUT_ENABLED=true` is an **opt-in restriction**: high-impact events that name the
instrument's currencies refuse **new** entries inside `[event - before, event + after]`
(`news_blackout` in the risk reasons). It adds a protection and never relaxes one, open positions
are untouched, and an unknown instrument (no resolvable currencies) is never "protected" by a
guess. Ingestion never raises into the loop: failures are reported in `stats.news` and audited.

## Research → runtime promotion (Phase 6)

The research runner already produces `data/research_report.json`; the promotion bridge is how a
validated candidate can reach the runtime — explicitly, with a human name attached.

```bash
# what the research produced, and which candidates its OWN gates passed
python scripts/promote_candidate.py list --eligible-only

# approve one (refused when the research did not validate it; records who approved it)
python scripts/promote_candidate.py approve --label "Donchian 30" --by "operator"

# the runtime uses it only when pointed at the file
PROMOTION_CONFIG_PATH=./data/active_strategies.json
```

Eligibility is the report's own verdict (`validation_passed` with no `rejection_reasons`), plus a
symbol/timeframe match and the parameters actually constructing a registered strategy. This bridge
adds no gates and lowers none; the metrics and gate outcomes are copied verbatim for the reviewer.
The approval file carries only strategy identity, parameters, approvals and provenance — no risk
limits, no credentials (`tests/test_promotion.py` asserts that). The promotion `version_id` is
stamped onto the strategy, so every outcome row can be traced back to the decision that approved it.

## Autonomous web research (Phase 5)

`python scripts/run_web_intel.py --from-settings` (or `--url ... --allow ...`) reads allow-listed
pages through a byte-capped, timeout-bounded, non-redirecting fetcher, stores findings with URL,
fetch time and content hash (deduplicated by hash), and prints the hypotheses it derived.

Research has **no path to configuration**: findings are notes and questions, `proposed_change_type`
is always `None`, and evidence quality stays `insufficient` until a candidate passes the normal
backtest → walk-forward → out-of-sample gates. Budgets are structural (`WEB_INTEL_MAX_SOURCES`,
`WEB_INTEL_MAX_BYTES_PER_PAGE`, `WEB_INTEL_MAX_TOTAL_BYTES`, `WEB_INTEL_TIMEOUT_S`), an empty
allow-list means nothing is ever fetched, and the whole layer is off by default.

## Operational resilience (Phase 7)

```powershell
# register the self-healing watchdog (logon + every 5 minutes)
.\deploy\windows\register-watchdog.ps1 -BotDir C:\mt5bbot
schtasks /Query /TN MT5-Watchdog /V /FO LIST
```

`deploy/windows/watchdog.ps1` probes `/health`; if the API is down it starts it, waits, and re-probes
(exit 1 if it never came back, 2 when the environment is broken). It is single-owner aware: the
runtime is only ever (re)started through `POST /api/v1/runtime/start`, never as a second process, so
two loops can never fight over the account. Decisions are appended to `logs/watchdog.log` and the
API token is read from `.env` at run time — never written into a task definition or logged.

## Strategy families (Phase 7)

`atr_breakout`, `ema_adx_trend`, `bollinger_reversion`, `rsi_ema_pullback`, `session_breakout`,
`mtf_trend` and `structure_breakout` join the registry, so `STRATEGIES=` and the research runner can
select them. Each one declares its parameters, attaches a stop loss, reports "not enough history"
instead of guessing, and decides only from bars it has already closed (the bar being evaluated is
recorded *after* the decision). `bar_to_event` now carries the bar's OHLC in the event metadata, so
range-based families see the same bars in backtest and live.

A real-data sanity check (not validation) on the committed 23,627-bar XAUUSDm M15 file, one pass
each, 0.26 spread + 0.05 slippage, 1% risk:

| family | trades | profit factor | win rate |
| --- | --- | --- | --- |
| `ema_adx_trend` (12/26, ADX 20) | 111 | 1.31 | 42.3% |
| `structure_breakout` (2/2, R2) | 87 | 1.23 | 40.2% |
| `rsi_ema_pullback` (EMA50, RSI14) | 175 | 1.04 | 36.0% |
| `session_breakout` (ORB 4, 07–16 UTC) | 86 | 1.04 | 50.0% |
| `mtf_trend` (H1/M15) | 87 | 0.97 | 34.5% |
| `atr_breakout` (20, buffer 0.25) | 17 | 0.53 | 23.5% |
| `bollinger_reversion` (20, 2σ) | 34 | 0.49 | 26.5% |

These are single-pass numbers on one year of one symbol — a starting point for the research runner,
not evidence of edge, and nothing here is promoted automatically.

**What the research actually found (2026-09-22, multiplicity-controlled):** running all 26 candidates
through the full stack (OOS split → walk-forward → Monte Carlo → spread 1x/2x → parameter
perturbation → Benjamini-Hochberg at α=0.10) produced **zero survivors**. Eleven candidates cleared
the raw gate stack, which is exactly the blind spot the correction exists for; the best raw p-value
was 0.176 (Donchian 50) against the ~0.0038 needed to survive a 26-candidate family. Conclusion: none
of these families — nor the currently-configured Donchian 20 — has a demonstrated edge on this data.
The system's value right now is the forward record it is building, not a P/L claim.

## Research quality: multiplicity control (Phase 8)

Running 25 candidates and keeping whoever looks best is how backtests lie. `research/multiplicity.py`
gives every candidate a sign-flip permutation p-value on its **out-of-sample** per-trade R multiples
and then corrects the whole family (Benjamini-Hochberg by default, Bonferroni available):

```bash
python -m mt5_platform.research.runner                    # BH at alpha=0.10 (default)
python -m mt5_platform.research.runner --alpha 0.05 --method bonferroni
python -m mt5_platform.research.runner --only Donchian     # subset while iterating
```

A candidate that clears every other gate but is not a family-corrected survivor is marked failed with
`fails_multiplicity_control`, and `scripts/promote_candidate.py` refuses reports that predate the
correction (`multiplicity_not_evaluated:rerun_research`). This adds a gate; it never lowers one.

The test is two-sided, deterministic for a given seed, and a candidate with too few closed trades gets
`p_value=None` and can never be a survivor — absence of evidence is not evidence. `tests/test_multiplicity.py`
also pins the *rate*: on pure noise at most ~15% of candidates look significant, and when fed 25 noise
series the corrected family yields **zero** survivors even though the raw rule flags some.

## Measuring coverage: which bucket earns (Phase 8)

`historical/attribution.py` groups recorded outcomes by strategy, regime, session, side and exit cause
and grades each bucket with the same thresholds the evidence engine uses — below 10 closed trades a
bucket is explicitly `insufficient_evidence` and gets no verdict. The snapshot exposes it as
`stats.attribution`:

```json
{"ledger_outcomes": 42,
 "coverage": {"strategies_recorded": 2, "measured": 1, "unmeasured": 1, "closed_trades": 42,
              "unattributed_trades": 0, "unmeasured_keys": ["mtf_trend"], "min_sample_weak": 10},
 "strategy": [{"key": "ema_adx_trend", "sample_size": 30, "expectancy_r": 0.14}],
 "regime":   [{"key": "trending", "sample_size": 40}]}
```

This is the number to watch when adding coverage: `unmeasured` counts strategies that trade but cannot
yet be judged, and `unattributed_trades` counts trades whose strategy/regime was never recorded (a
data problem, not a market one). A bucket containing any backtest outcome is flagged
`contaminated: true` — replay results are not forward evidence.

## Safety

- Default `TRADING_MODE=demo`
- Live mode refuses to boot without dual acknowledgment flags
- Mock adapter for tests; the `mt5` backend refuses real-money accounts until live is acknowledged
- Signals never become orders without a risk approval; kill switch blocks submission
- Risk math uses the executable price (`execution_entry` from the live quote), not the stale signal entry
- Broker state is truth: reconciliation corrects local order state and audits every mismatch
- Do not bypass risk or order-management layers from ingestion workers
- Windows deployment: see `docs/RUNBOOK-AWS.md` (one-machine setup, Task Scheduler autostart, HTTPS)
