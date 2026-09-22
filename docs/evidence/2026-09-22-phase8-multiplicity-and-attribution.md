# Phase 8: multiplicity control, the research verdict, and coverage attribution

Date: 2026-09-22
Mode: demo only. No gate, limit or threshold was lowered; one gate was **added** to research and
promotion.

## Why

"Add more strategies to get more opportunities" only works if the extra candidates are *validated*.
Twenty-six candidates on one year of M15 gold is a search, and a search over enough candidates will
always produce something that looks profitable. Nothing in the pipeline accounted for that.

## What was built

### Multiplicity control (`src/mt5_platform/research/multiplicity.py`)
- `sign_flip_permutation`: two-sided permutation p-value on **out-of-sample** per-trade R multiples
  (in-sample results already picked the candidate, so testing them would be circular). Deterministic
  per seed, add-one smoothed, two-sided, and `p_value=None` for fewer than 20 trades — a candidate
  with no testable evidence can never be a survivor.
- `benjamini_hochberg` (default, FDR) and `bonferroni` (family-wise, kept for comparison).
- `apply_multiplicity` corrects a family and keeps untestable candidates out of it.

### Runner integration (`research/runner.py`)
- `CandidateReport` gains `oos_mean_r`, `p_value`, `p_value_adjusted`, `multiplicity_survivor`,
  `multiplicity_method`, `n_permutations`; the report gains a `multiplicity` block.
- `apply_multiplicity_pass` folds the family verdict into `validation_passed`: a candidate that
  cleared every other gate but is not a survivor is marked failed with
  `fails_multiplicity_control:p_adj=…:alpha=…`.
- CLI: `--alpha`, `--method`, `--only`, `--permutations`, `--seed`, `--csv`, `--out`.

### Promotion bridge (`research/promotion.py`)
- Promotion now requires the family-corrected verdict. A report generated before the correction (or a
  candidate the correction could not test) is refused with `multiplicity_not_evaluated:rerun_research`.
- The raw and adjusted p-values travel into the approval record for the reviewer.

### Coverage attribution (`src/mt5_platform/historical/attribution.py`)
- Groups recorded outcomes by strategy / regime / session / side / exit cause; every bucket carries
  its own sample size, evidence grade (same thresholds as the evidence engine), expectancy (% and
  money), expectancy in R, total P/L and share.
- Below 10 closed trades a bucket is `insufficient_evidence` and is given **no verdict**.
- `unattributed` / `unlabelled` buckets are surfaced rather than dropped (a data problem, not a
  market one), and any bucket containing backtest outcomes is flagged `contaminated`.
- `coverage_summary` reports `measured` vs `unmeasured` strategies — the number to watch when adding
  coverage.
- Exposed live as `stats.attribution` in the runtime snapshot.

## Evidence

- `pytest -q` → **661 passed** (24 new across `tests/test_multiplicity.py`, `tests/test_attribution.py`
  plus the updated promotion tests); `ruff check .` clean.
- Permutation test size pinned by test: on 120 pure-noise series at most ~15% look significant at the
  nominal 5%; on 25 pure-noise candidates the corrected family yields **zero** survivors while the raw
  rule flags some.
- Full research run over all 26 candidates (`python -m mt5_platform.research.runner`, 23,627 real
  XAUUSDm M15 bars, 0.26 spread, 0.05 slippage, 1% risk):

  | | count |
  | --- | --- |
  | candidates | 26 |
  | survived the raw gate stack (OOS + WF + MC + spread + perturbation) | 11 |
  | survived Benjamini-Hochberg (α = 0.10) | **0** |
  | untestable (too few OOS trades to test) | 0 |

  Best raw p-value: `Donchian 50` at 0.176 (OOS 204 trades, PF 1.23) against the ≈0.0038 needed to
  survive a 26-candidate family at α = 0.10. Every adjusted p-value is 1.000 (BH caps there).
- Consequence, verified: `python scripts/promote_candidate.py list` → **26 candidate(s), 0 eligible
  for approval**; every previously-"passing" candidate now carries
  `fails_multiplicity_control:p_adj=1.000:alpha=0.1`.

## Interpretation

The honest reading is not "the strategies are bad" but "this data cannot distinguish them from
noise". That is the answer to "how do we get more opportunities?" — not by switching more families
on. What would change the answer: more data (multi-year, more than one instrument), hypotheses that
are conditioned on something measurable (regime, session, volatility state), and cost assumptions
that match the live spread. Until then the running system's product is its forward ledger, which the
attribution layer now measures per bucket without any reliance on the backtest search.

## Non-negotiables held

Demo only; LIVE untouched; no gate/limit/threshold lowered (multiplicity is an added gate); no
automatic configuration change; `.env` untracked.
