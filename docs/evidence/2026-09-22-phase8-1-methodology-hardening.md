# Phase 8.1: research methodology hardening

Date: 2026-09-22
Predecessor: Phase 8 (`3a1c9ac`) — multiplicity control + attribution.
Mode: demo only. No gate, limit or threshold lowered; two gates **added** (effective sample) and one
dataset boundary **introduced** (sealed holdout) that removes data from selection.

## Why

The Phase 8 result (26 candidates, 11 raw survivors, 0 after correction) was correct but the
experiment behind it was not yet auditable: the null hypothesis was implicit, raw trade counts were
read as if they were independent observations, there was no sealed data, the search family was not
recorded, and the run could not be reproduced. Getting 100,000 trades first and *then* discovering
that the test assumptions were never documented was the risk being removed here.

## What was built

### 1. The statistical contract (`research/methodology.py`)
- H₀ (`expected per-trade R <= 0`), statistic (mean R), null draws (sign flips), sidedness (two-sided),
  permutations, seed, minimum trades — all exported by `test_description` and embedded in every
  report's `methodology` block.
- Six assumptions with their treatment, one of them explicitly **not testable** here
  (`iid_permutation_draws`, whose *size* is instead calibrated on synthetic noise by unit test).

### 2. Dependence diagnostics — raw trades ≠ sample size
`dependence_diagnostics` reports overlap pairs, overlap fraction, mean/max concurrency, trades-per-day
Fano factor, lag-1 autocorrelation, Wald–Wolfowitz runs z, `n_eff_ar1`, `n_eff_overlap` and the
conservative `n_effective` (minimum of the two), with explicit flags for each detected violation.
Real examples from the run: `EMA 12/26 ADX20` OOS 267 trades → `n_effective` 210 (72 overlapping
pairs, mean concurrency 1.27); `Donchian 20` 128 → 110.

### 3. Effective-sample gate (added restriction)
A candidate whose `n_effective` is below the minimum test sample cannot be validated:
`insufficient_effective_sample:n_eff=…<20`. The permutation p-value does not yet model dependence, so
this makes that admission load-bearing instead of decorative.

### 4. Power reporting
Per candidate and per family: median `n_effective`, smallest detectable edge in R at the family's
smallest-rank threshold, and the independent trade count needed for a 0.05R edge. Reported next to
every p-value so "we found nothing" can be read as "this sample cannot see small edges".

### 5. Hypothesis family records (`research/manifest.py`)
`family_id`, `candidate_id`, `symbol`, `timeframe`, `regime_definition`, `parameter_set`,
`hypothesis_version` for every candidate, embedded in the report's `family` block. `family_id`
derives from symbol + timeframe + hypothesis version + code commit.

### 6. Sealed final holdout
DISCOVERY / VALIDATION / FINAL HOLDOUT split with the most recent 15% sealed (3,544 bars,
2026-07-29 → 2026-09-21 on the current file, SHA-256 recorded) and excluded from selection.
`--confirm-holdout` runs the same candidates on it **once**, recording dataset + holdout hashes in
`data/research_holdout.json`; a repeat for the same family and dataset raises unless `--force-holdout`
(replacing a known-void record). A changed dataset is detectable, so a confirmation cannot be silently
re-run against a different slice.

### 7. Corrections: primary + labelled sensitivity
`benjamini_yekutieli` added (FDR under arbitrary dependence) and `sensitivity()` reports BH, BY and
Bonferroni side by side. BH remains the primary verdict; BY is documented as a sensitivity analysis,
not as a replacement whose purpose would be a friendlier answer.

### 8. Manifest and reproducibility
`data/research_manifest.json` records dataset/research hashes, bar counts, date range, symbol,
timeframe, costs, backtest settings, family, statistical contract, correction method and α, holdout
state, git commit, Python/platform and the report hash. `--manifest` re-runs the experiment from it
after verifying the dataset hash, refusing if the data changed.

### 9. Surfaces
`GET /api/v1/research/status` plus a dashboard **RESEARCH STATUS** card (candidates searched / raw
survivors / multiplicity survivors / promotable, correction + sensitivity, sealed holdout, power, live
state, provenance) and a **FORWARD EVIDENCE** table (per strategy: trades, grade, expectancy R, win
rate, verdict with `insufficient` below 10 closed trades). With no report the card says NOT RUN.

### 10. Language
Report conclusions, README and docs now say *failure to reject the null at the multiplicity-adjusted
threshold* rather than implying the strategies were proven to be noise.

## Evidence

- `pytest -q` → **687 passed** (26 new: `test_methodology.py` 11, `test_manifest.py` 10,
  `test_research_status.py` 3, plus multiplicity/manifest additions); `ruff check .` clean;
  `npm run build` exit 0.
- End-to-end smoke run (`--only Donchian --only 'EMA 12/26'`): report written with `methodology`,
  `family`, `holdout`, `manifest`, `sensitivity`, `conclusion`, `report_sha256`; per-candidate
  `n_eff` and `DEP` flags printed; conclusion text carries the corrected interpretation.
- Holdout on the committed dataset: 23,627 bars → research 20,083 (discovery 15,062 / validation
  5,021) + sealed 3,544 (hash `64e1377528…`). Nothing has touched it yet.
- Unit tests pin the test's *size* (≤15% of pure-noise series look significant at the nominal 5%; 25
  noise candidates yield zero survivors after correction) and the diagnostics' behaviour on overlapping,
  streaky and clustered trade series.

## Non-negotiables held

Demo only; LIVE untouched; no threshold lowered (multiplicity, effective-sample gate and the holdout
boundary all *remove* candidates or data from selection); no automatic promotion; `.env` untracked.
