# Research methodology (Phase 8.1)

This document is the contract the research pipeline holds itself to. It exists so that a result can be
audited years later without re-reading the code, and so that "we tested it" can never quietly mean
"we looked at it until it agreed with us".

## 1. The null hypothesis and the test

| | |
| --- | --- |
| **H0** | the candidate's expected per-trade R is ≤ 0 (no positive edge) |
| **Statistic** | mean per-trade R over the evaluation window |
| **Null draws** | random sign flips of the observed \|R\| values (under H0 the sign of each trade is exchangeable) |
| **Sidedness** | two-sided — a systematically *losing* candidate is also distinguishable from chance, and that is useful information about the hypothesis |
| **Test window** | the out-of-sample slice only: in-sample results were used to pick the candidate, so testing them would be circular |
| **Permutations / seed** | recorded in the report and the manifest (default 2000, seed 42) |
| **Minimum trades** | 20; below that `p_value = None` and the candidate can never be a survivor |

Implementation: `src/mt5_platform/research/multiplicity.py`. Deterministic for a given seed, add-one
smoothed (`p = (extreme + 1) / (permutations + 1)`, so p is never claimed to be 0), and the family is
corrected afterwards with Benjamini–Hochberg (α = 0.10 by default).

### Interpretation rule

Failing to reject H0 is **not** evidence that no edge exists. It means this data, at this sample size,
could not distinguish the candidate from chance. Every p-value is therefore reported next to that
candidate's power block.

## 2. Assumptions, and what is done about each

`research/methodology.py::ASSUMPTIONS` is the machine-readable source of this table, and it is
embedded in every report.

| id | assumption | treatment |
| --- | --- | --- |
| `exchangeability` | the sign of a trade's R is exchangeable under H0 | measured: overlap, clustering and serial dependence reported per candidate |
| `serial_dependence` | residuals are not serially dependent (ρ₁ ≈ 0) | measured: lag-1 autocorrelation + Wald–Wolfowitz runs test; `n_eff_ar1` reported |
| `non_overlapping_trades` | trades do not overlap in time | measured: overlap fraction, mean/max concurrency; `n_eff_overlap` reported |
| `independent_outcomes` | outcomes are not clustered so one market move is counted several times | measured: trades-per-day Fano factor |
| `costs_included` | spread, slippage and commission are already in each trade's R | enforced: the backtest applies configured costs; cost sensitivity is a gate |
| `iid_permutation_draws` | permutation draws are independent and the p-value is uniform under H0 | **not** directly testable here; test *size* is calibrated on synthetic noise by unit test |

## 3. Raw trades are not a sample size

`dependence_diagnostics` (same module) reports, per candidate: `n_raw`, `overlap_pairs`,
`overlap_fraction`, `max_concurrency`, `mean_concurrency`, `trades_per_day_max`,
`trades_per_day_mean`, `fano_factor`, `autocorr_lag1`, `runs_z`, `n_eff_ar1`, `n_eff_overlap`,
`n_effective`, `flags`.

`n_effective` is the conservative minimum of two explicitly approximate corrections:

* **AR(1)**: `n · (1 − ρ₁)/(1 + ρ₁)`, clipped to `[1, n]` — we never claim more information than raw
  trades, even when negative autocorrelation suggests there is some;
* **overlap**: `n / mean_concurrency`.

The permutation p-value **does not yet account for dependence**; the report says so in its note field,
and the gate below makes that admission load-bearing.

**Effective-sample gate.** A candidate whose `n_effective` is below the test minimum cannot be validated
(`insufficient_effective_sample:n_eff=…<20`), because its p-value would be optimistic rather than
conservative. This only ever removes candidates.

## 4. Power: what the sample could have detected

`power_analysis` reports, for every candidate and for the family as a whole, the smallest mean-R edge
the effective sample could distinguish from chance at the family's smallest-rank threshold (α/m). With
`σ_R ≈ 1R` (R is normalized by the stop distance), the null SD of the mean is `σ_R/√n_eff`, so:

```
smallest detectable edge            ≈ z_(α/m) · σ_R / √n_eff
independent trades needed for edge e ≈ (z_(α/m) / e)²
```

Worked example from the 2026-09-22 run (m = 26, α = 0.10 → z ≈ 2.89): a 0.05R edge needs ≈ 3,400
independent trades. The OOS windows in that run carried `n_eff` ≈ 166 (median), which bounds the
smallest detectable edge to ≈ 0.18R. That is why the honest reading of "0 survivors" is *this data
cannot see small edges*, not *there is no edge*.

## 5. The search family, recorded

Every candidate is stored with `family_id`, `candidate_id`, `symbol`, `timeframe`,
`regime_definition`, `parameter_set` and `hypothesis_version` (`research/manifest.py`), so the
multiplicity report states the search it corrected for. `family_id` derives from symbol, timeframe,
hypothesis version and code commit: the same code and hypotheses on the same instrument share a family,
and any of those changing starts a new one.

Corollaries enforced by convention:

* parameter variants of one idea (`lookback` 20, 21, 22, …) belong to the same family and raise its
  size, so they cost power;
* adding symbols multiplies hypotheses — every symbol × candidate pair is a test, and the family must
  be declared accordingly;
* a smaller family is a legitimately cheaper experiment, and is preferable to a large shallow one.

## 6. Dataset roles and the sealed holdout

```
DISCOVERY (older bars)      develop hypotheses
    ↓
VALIDATION (recent bars)    select candidates — the IS/OOS split lives here
    ↓
FINAL HOLDOUT (most recent) SEALED: one confirmation run, never used for development
```

* The runner seals the most recent `--holdout-fraction` (default 0.15 = 3,544 bars on the current file:
  2026-07-29 → 2026-09-21) and reports its SHA-256. Selection runs only on discovery + validation.
* `--confirm-holdout` runs the same candidates on the sealed slice **once** and records the result in
  `data/research_holdout.json` together with the dataset and holdout hashes. A second confirmation for
  the same family *and* dataset is refused (`--force-holdout` exists only for a known-void record).
* Because the holdout hash is stored, a changed dataset is visible: a confirmation can never be
  silently re-run against a slightly different slice.
* The holdout can never flow back into development. Once you look at it, it becomes evidence about the
  research *process*, not another dataset to optimise against.

## 7. Corrections: one primary, the rest labelled sensitivity

* **Primary:** Benjamini–Hochberg at α (default 0.10) — controls the expected proportion of false
  discoveries among rejections, and is the verdict that gates promotion.
* **Sensitivity:** Benjamini–Yekutieli (FDR under *arbitrary* dependence, more conservative) and
  Bonferroni (family-wise). Reported side by side, **never** used to pick a friendlier answer.
* Correlated tests do not automatically invalidate BH: under positive dependence it can still control
  FDR, often conservatively. Our candidates are strongly correlated (nested parameter variants), which
  makes BH *conservative* here — a reason to report BY, not to switch to it hoping for survivors.

## 8. Manifest and reproducibility

`data/research_manifest.json` (also embedded in the report) records: dataset path + SHA-256, research
slice SHA-256, bar counts, date range, symbol, timeframe, spread/slippage/commission, backtest
settings, the full candidate family, the statistical contract (H₀, statistic, permutations, seed,
minimum trades), the correction method and α, the holdout state, the git commit, Python/platform, and
the report's own SHA-256.

Reproducing a run:

```bash
python -m mt5_platform.research.runner --manifest data/research_manifest.json
```

The runner loads the experiment from the manifest, **verifies the dataset hash first** and refuses to
run if the data changed. A different dataset is a different experiment, and must be reported as one.

## 9. Promotion rule

A candidate can only be promoted when, in order: it passed the ordinary gates; the effective-sample
gate; and it is a survivor of the primary multiplicity correction. Reports written before the correction
existed are refused (`multiplicity_not_evaluated:rerun_research`). Promotion additionally requires a
named human approver, and the promotion version is stamped onto the strategy so every outcome traces
back to the decision.

Forward evidence is the last mile: a research survivor is configured on demo and must then earn
attribution buckets (`insufficient` below 10 closed trades, `weak` at 10, `moderate` at 30, `strong`
at 100) before it is treated as anything more than a hypothesis under test.
