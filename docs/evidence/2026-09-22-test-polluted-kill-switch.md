# 2026-09-22 — kill switch was engaged by the test suite, not by the market

## Why this file exists

The DEMO runtime was halted (no orders) while it appeared to be `RUNNING`. Investigation showed the
engage reason was not a market or account event: `pytest` runs were persisting kill-switch state
into the live `risk_state.json`. This file preserves that evidence; the fix is `tests/conftest.py`,
which makes the suite blind to `.env` and to live runtime state.

## The polluted state (verbatim)

```json
{"kill_switch": true, "halt_reasons": ["test_halt", "manual", "max_drawdown_exceeded",
 "trading_loop_errors", "manual", "max_drawdown_exceeded", "trading_loop_errors"],
 "paused": false, "pause_reasons": []}
```

- file: `C:\mt5bbot\risk_state.json`
- sha256: `b67cc3de9050f9143cd546bfc179eea8218292a6dfdd395199d68e91adddf22b`
- backup copy: `docs/evidence/risk_state.polluted-20260922T111051Z.json`

## Attribution of every halt reason

| reason | produced by |
| --- | --- |
| `test_halt` | `tests/test_phase6.py::test_kill_switch_blocks_submission` |
| `manual` | `tests/test_phase5.py::test_risk_engine_pause_kill_and_stats` |
| `max_drawdown_exceeded` | `tests/test_runtime.py::test_daily_loss_breach_engages_kill_switch_via_monitor` |
| `trading_loop_errors` | `tests/test_runtime.py::test_repeated_errors_halt_the_loop_and_engage_kill_switch` |

Every reason maps to a literal test fixture string, and the middle three appear twice — i.e. two
separate polluted test runs. No reason was unexplained.

## Proof that no genuine breach existed at reset time (read-only broker snapshot)

| metric | value | configured limit | breach |
| --- | --- | --- | --- |
| drawdown | 0.0002% | 10.00% | no |
| daily loss | 0.00% | 3.00% | no |
| margin level | 28,951,937% | min 20% | no |
| open positions | 1 (manual `3264036722`, untouched) | — | — |

Preconditions confirmed before clearing: no runtime process was running (no `python`/`caddy`
process, nothing listening on :80/:8000) and the polluted state matched the reported content
exactly.

## What was reset, and what was deliberately left alone

Cleared: the stale kill switch only — the state file was rewritten to the canonical schema
`{"kill_switch": false, "halt_reasons": [], "paused": false, "pause_reasons": []}` and then
re-loaded through `RiskEngine` to prove the gate opened.

Untouched: risk limits (`MAX_DRAWDOWN_PCT`, `MAX_DAILY_LOSS_PCT`, `MAX_SPREAD_POINTS`, sizing),
validation gates for LIVE, `TRADING_MODE=demo`, `LIVE_TRADING_ENABLED=false`, and the kill-switch
mechanism itself.
