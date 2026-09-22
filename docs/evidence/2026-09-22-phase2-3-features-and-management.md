# Phase 2 + 3 — candle-shape features and dynamic in-trade management (2026-09-22)

## Phase 2 — candle-shape features

`historical/features.py::compute_candle_shape` turns the closed candles the runtime actually had
into a bounded, versioned snapshot (`CANDLE_FEATURE_VERSION = "1.0"`):

| field | meaning |
| --- | --- |
| `body_ratio`, `upper_wick_ratio`, `lower_wick_ratio` | candle anatomy of the last closed candle |
| `close_position_in_range` | 0 = closed at the low, 1 = at the high |
| `range_vs_window_median` | last range against the median range of the window |
| `gap_pct` | open versus the previous close |
| `consecutive_same_direction` | how many candles in a row closed the same way |
| `volume_ratio` | last volume against the window median volume |
| `window_high`, `window_low` | extremes of the captured window |
| `pattern`, `direction` | coarse rule labels: marubozu / long_body / doji / hammer / shooting_star / normal; bull / bear / flat |

Rules that keep it honest:

* the window is bounded (default 20 candles) and read in chronological order — no lookahead;
* every value is `None` when it cannot be computed (no volume history, no previous close, no
  preceding ranges) instead of being zero-filled;
* a dominant single wick is classified before the doji rule, so a hammer is not swallowed as a
  doji (this was caught by a test during development);
* the version string is stored inside the snapshot, so a future feature change cannot silently
  reinterpret historical trades.

Wiring: the loop keeps a 60-bar ring buffer per symbol (seeded from the warm-up history, appended
on every newly closed candle) and passes it to the recorder when a position is first observed.
The snapshot therefore describes the candles that existed *before* the entry.

## Phase 3 — dynamic in-trade management

* `INTELLIGENCE_ENABLED=true` → the loop builds a market context from live ticks every cycle and
  hands each open position to the PositionManager (thesis validity, trailing, break-even,
  reductions, exits). Every decision still passes RiskEngine → OrderManager → the broker.
* `INTELLIGENCE_ENTRIES_ENABLED=false` (default) → the agent/synthesis path may **not** propose new
  entries, so the strategy registry remains the entry source of record. Turning management on does
  not silently add a second entry source.
* Exit causes come from the decision that performed the exit and are recorded on the outcome
  (`thesis_invalidation`, `trailing_stop`, `break_even`, …), with exit price and money taken from
  the broker's closing deals (`position_close_details`) rather than from the response body.
* The loop publishes an in-trade trace (`stats.position_management`): last decision
  (outcome/action/ticket/reason), counters per outcome and action, intelligence counters and the
  entry-gate state. Rendered by the dashboard's "In-trade management" card.
* A latent crash was fixed on the way: `_manage_position` dereferenced the intelligence layer
  unconditionally when counting exits.

## Verification

* `pytest -q` → 571 passed (22 new: 14 feature tests, 8 management tests).
* `ruff check .` → all checks passed. `npm run build` → exit 0.
* Live: runtime restarted with `INTELLIGENCE_ENABLED=true` and `INTELLIGENCE_ENTRIES_ENABLED=false`
  — contexts and position evaluations run, entry generation stays gated, no forced trades, demo
  only, kill switch clean.

Note: with the terminal's AutoTrading still off (previous finding), an in-trade exit decision will
be computed and audited but cannot be transmitted; the execution card shows
`terminal_autotrading_disabled` (10027) until AutoTrading is enabled, and broker-side SL/TP keep
protecting open positions in the meantime.
