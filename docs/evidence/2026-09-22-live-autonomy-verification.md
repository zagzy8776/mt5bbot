# 2026-09-22 — live autonomy verification (DEMO)

## 1. The real root cause of "0 signals" (two independent blockers)

The dashboard showed `RUNNING` with `Signals: 0 / Orders sent: 0` for two separate reasons:

**Blocker 1 — the kill switch was engaged by the test suite** (see
`docs/evidence/2026-09-22-test-polluted-kill-switch.md`). Every entry was rejected with
`emergency_kill_switch`.

**Blocker 2 — the runtime was asking the broker for a symbol that does not exist.** The live
pipeline trace exposed it:

```json
{"cycle": 12, "stage": "no_candle_available", "symbol": "XAUUSDM", "last_processed_bar": null}
```

The configured symbol is `XAUUSDm`. `TradingLoop.__init__` upper-cased every symbol, so
`copy_rates_from_pos("XAUUSDM", M15, ...)` returned nothing, `latest_closed_bar()` returned
`None`, and the loop evaluated **zero candles forever** while reporting itself as healthy.
The earlier "symbol case" fix covered the adapter, events, signals, orders, strategies and the
validation gates — but not the loop, which is where the bug survived.

Fixed in `runtime/loop.py` (`self.symbols = [s.strip() for s in symbols]`) with the missing
regression guard in `tests/test_symbol_case.py::test_loop_and_feed_use_the_exact_broker_symbol`
(it asserts the loop keeps `XAUUSDm` and that the feed actually receives closed candles).

## 2. What the live runtime does now (after the fix)

| field | value |
| --- | --- |
| state / connected | `running` / `true` |
| symbol / timeframe | `XAUUSDm` / `M15` |
| kill switch | `false` (clean state, limits untouched) |
| warm-up replay | 200 bars, 61 signals discarded (never traded) |
| first live candle | 11:15 closed bar, `evaluations: 201` (previous instance) |
| second live candle | 11:30 closed bar, stage `candle_evaluated_no_setup` |
| third live candle | 11:45 closed bar, stage `candle_evaluated_no_setup` |
| orders | 0 (no entry condition met on those candles; nothing was forced) |

Live trace for the 11:45 candle (strategy decisions are recorded, not hidden):

```json
{"cycle": 289, "stage": "candle_evaluated_no_setup", "symbol": "XAUUSDm",
 "bar_time": "2026-09-22T11:45:00+00:00", "bar_open": 4317.747, "bar_close": 4322.359,
 "bar_high": 4323.373, "bar_low": 4316.683, "last_processed_bar": "2026-09-22T11:45:00+00:00",
 "poll_s": 5.0, "waiting": true}
```

`evaluations: 202` = 200 warm-up bars + 2 live candles; `signals: 47` are all warm-up replay
signals (never traded); `signals_rejected: 0`; `orders_sent: 0`; `errors: 0`.
Breakout fires on roughly a quarter of M15 candles, so two candles without a setup is normal —
nothing was manufactured and no threshold was touched.


Raw observation rows: `logs/observe-runtime.log` (git-ignored runtime log).

## 3. Test isolation (proved twice)

`_isolation_check.py` captured state, ran `pytest -q` twice, and compared:

```
pytest run 1: exit=0 :: 480 passed in 3.92s
pytest run 2: exit=0 :: 480 passed in 4.06s
OK risk_state_sha / risk_state / env_sha / runtime_state / runtime_candles
OK runtime_orders / runtime_connected / positions
db counts unchanged: orders 1, executions 1, signals 170, positions 0, audit_events 170, account_snapshots 1
```

`tests/conftest.py` makes the suite blind to `.env` (its env file is disabled for the test
process) so a test run can never read — or write — the live `risk_state.json`, the live account,
the running runtime, the production `.env`, or the database.

## 4. Strategy research (unchanged gates, existing engine)

`src/mt5_platform/research/runner.py` on 23,627 bars of real XAUUSDm M15 (2025-09-21 → 2026-09-21),
with IS/OOS split, 5-window walk-forward, 300-path Monte Carlo, 0/1/2x spread sensitivity and
parameter perturbation: **7 of 17 candidates passed**.

| candidate | IS PF | OOS PF | walk-forward | MC profitable | spread | params |
| --- | --- | --- | --- | --- | --- | --- |
| Donchian 20 (`breakout`, lookback 20) | 1.13 | 1.08 | 1.00 | 95% | stable | stable |
| Donchian 30 | 1.09 | 1.12 | 1.00 | 92% | stable | stable |
| Donchian 50 | 1.16 | 1.23 | 1.00 | 98% | stable | stable |
| SMA 10/50 | 1.07 | 1.09 | 0.75 | 79% | stable | stable |
| SMA 5/50 | 1.07 | 1.08 | 0.75 | 83% | stable | stable |
| Momentum lb10 0.5% | 1.04 | 1.02 | 1.00 | 70% | stable | stable |
| **SMA 5/20 (was live by default)** | 1.02 | **0.86** | **0.50** | **34%** | **unstable (0/5)** | stable |
| MeanRev w20 z2.0 (worst) | 0.82 | 1.02 | 0.25 | 5% | unstable | unstable |

Consequence applied to the DEMO runtime: `STRATEGIES=breakout` only. That removes the one live
strategy that fails the robustness gates; it lowers no gate and does not touch
`validation_report.json` (still failing, so LIVE stays blocked).

Candidate families that are **not implemented** in the registry yet (so they could not be
researched): ATR breakout, EMA+ADX trend, Bollinger mean reversion, RSI+EMA pullback,
London/session breakout, previous-day high/low breakout, MTF trend + M15 entry, volatility-regime
breakout, structure breakout/retest. These remain the research backlog.

## 5. Observation / caveats

* The manual position `3264036722` counts towards duplicate/parallel-position limits, so an
  autonomous BUY on XAUUSDm is rejected as `duplicate_position` while it is open.
* No autonomous order had been submitted at the time of writing: on the candles evaluated so far
  the breakout condition was simply not met. Nothing was manufactured, and no gate was lowered.
  (The earlier warm-up-persistence caveat is resolved — see section 6.)

## 6. Warm-up/replay isolation (data-integrity fix)

Warm-up replays were correctly never traded, but they *were* persisted by the signal-store sinks,
so the operational database and dashboard filled with historical-timestamped signals that had
never been live. Fixed in `signals/__init__.py` + `runtime/loop.py`:

* `SignalEngine.on_market_data(event, replay=True)` marks a warm-up candle: replay evaluations and
  signals count into dedicated `replay_*` counters instead of the live ones.
* A replay signal is **tagged** (`metadata = {"session": "warmup", "replay": True}`), kept in a
  replay-only buffer (`SignalEngine.replay_signals()`), and is **never** handed to the store sinks,
  the audit-store sink, the live feed (`recent_signals()`), the "last live signal" field, or the
  live cooldown state — so a replay cannot influence or appear as live trading.
* Live signals take exactly the previous path: counted as live, persisted, exposed by
  `/api/v1/signals`.
* `reset_state()` clears the replay buffer/counters per warm-up session.

Live proof (same 200-bar warm-up producing 47 signals, old code vs new code):

| | old code | new code |
| --- | --- | --- |
| `signals` rows added by one warm-up | ~47 (170 accumulated) | **0** |
| `SIGNAL_GENERATED` audit rows added | ~47 (169 accumulated) | **0** |
| live counters | not separated | `evaluations: 0`, `signals_generated: 0` |
| replay counters | — | `replay_evaluations: 200`, `replay_signals_generated: 47`, `replay_persisted: false` |

Pollution written by the old code before the fix was removed after a read-only audit:
`DELETE 170` from `signals`, `DELETE 169` `SIGNAL_*` rows from `audit_events` (the audit proved all
170 signal rows were replay/test artifacts with historical timestamps — no live signal had ever
been stored). The `orders`, `executions` and `account_snapshots` test fixtures were left untouched.

Dashboard: the Signal Pipeline card now shows **Live evaluations / Live signals / Live rejections**,
plus **Warm-up replay (never traded or stored)** and **Replay counters (not live)** rows, and the
per-strategy table has separate live and replay columns.
