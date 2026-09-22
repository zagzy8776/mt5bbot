# Phase 1 — live trade recorder + historical outcome ledger (2026-09-22)

## Why

`HistoricalOutcome` existed as a model and the evidence engine already consumed it, but **nothing
in the runtime ever wrote one**. Consequences:

- the bot recorded no completed trades;
- the evidence ledger was permanently empty, so every historical query returned
  `insufficient evidence` forever, no matter how much the bot traded;
- `IntelligenceLayer.record_completed_trade()` (and with it DecisionMemory / PostTradeReview)
  was dead code.

This phase wires the missing producer and keeps the primary goal unchanged: autonomous
XAUUSDm **demo** trading, with the manual-position feature staying auxiliary.

## What was built

| Area | Where |
| --- | --- |
| Exit-cause taxonomy (`ExitCause`) + population/status enums | `common/enums.py` |
| `HistoricalOutcome` (extended), `TradeLeg`, `SetupFeatures` entry snapshot | `historical/models.py` |
| Deterministic MAE/MFE tracking | `outcomes/excursions.py` |
| Exit-cause resolution (explicit, level match, never P/L) | `outcomes/exit_cause.py` |
| Live recorder (open → track → partials → finalize → persist → retry) | `outcomes/recorder.py` |
| Review → decision memory → lesson proposals (no config change) | `outcomes/learning.py` |
| `outcomes` / `trade_legs` tables, store methods | `storage/models.py`, `storage/base.py`, `storage/__init__.py`, `storage/sqlalchemy_store.py` |
| Database → ledger → EvidenceEngine + quality reporting | `historical/outcome_loader.py` |
| Loop wiring (reconcile, quotes, partial/final exits, entry attribution) | `runtime/loop.py` |
| Service wiring (recorder, learning, ledger, dashboard stats) | `runtime/service.py` |
| Broker closing-deal P/L (`position_close_details`) | `execution/base.py`, `execution/mt5_adapter.py`, `execution/__init__.py` |
| Backtest → same schema | `backtest/outcomes.py` |
| `GET /api/v1/outcomes` + dashboard card | `api/__init__.py`, `frontend/src/lib/api.ts`, `frontend/src/App.tsx` |

## Invariants kept

- `TRADING_MODE=demo`, `LIVE_TRADING_ENABLED=false` (unchanged).
- No risk limit, spread limit, validation gate, kill-switch protection, position limit or
  confidence requirement was lowered. Evidence thresholds stay 10 / 30 / 100 and are reported,
  never adjusted.
- No automatic configuration change: lessons are proposed with `proposed_change_type=None` and
  `proposed_change={}`; promotion still needs research validation and an explicit version.
- The recorder cannot block or alter trading: every store interaction is wrapped, failures are
  queued for retry, and a broken database is proven not to stop the loop.
- Manual/external positions remain auxiliary and are recorded in their own population.

## Verification

- `pytest -q` → 536 passed (50 new tests across recorder, learning/evidence, live integration, API).
- `ruff check .` → all checks passed.
- `npm run build` → exit 0 (frontend build).
- `risk_state.json`, `.env` and `validation_report.json` are byte-identical before/after the whole
  suite; `.env` remains ignored and untracked.

Key behavioural tests:

- `tests/test_outcome_recorder.py` — BUY/SELL MAE/MFE, zero-movement and multi-excursion paths,
  exit-cause taxonomy, level matching without P/L, partial closes, idempotent finalize, restart
  recovery, orphan finalization, persistence failure/retry, deterministic replay, lossless store
  round trip (including SQLite schema), summary populations.
- `tests/test_outcome_learning.py` — backtest/live schema compatibility, DecisionMemory and
  PostTradeReview integration, `record_completed_trade` actually called, learning failure
  isolation, evidence quality progression 0 → insufficient, 10 → weak, 30 → moderate, 100 → strong
  with unchanged thresholds, population separation.
- `tests/test_outcome_live_integration.py` — loop → order → broker position → recorder → stop-out
  closed by the broker → finalized with real money from the closing deal → persisted → reviewed;
  plus "broken store does not stop trading" and "loop without a recorder behaves as before".
- `tests/test_api.py` — `/api/v1/outcomes` reports records, separated populations and honest
  evidence quality; the runtime snapshot carries the outcomes/learning/evidence blocks.
