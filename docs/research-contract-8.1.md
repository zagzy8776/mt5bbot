# RESEARCH CONTRACT 8.1

Status: **FROZEN**. This document is the constitution of the research engine. It is immutable: any
change to the values below requires a **new contract version (8.2, 9.0, …)** with its own document,
its own run and its own report. 8.1 keeps meaning what it meant when this report was produced.

Implemented by commit `1e381fd` (git `1e381fd3`), and verified: no file under
`src/mt5_platform/research` has changed since that commit produced the 8.1 report.

## Frozen settings

| setting | value |
| --- | --- |
| Primary correction | Benjamini–Hochberg |
| Alpha (α) | 0.10 |
| Sensitivity corrections (never the verdict) | Benjamini–Yekutieli (α=0.10), Bonferroni (α=0.05) |
| Primary test | two-sided sign-flip permutation test on out-of-sample per-trade R |
| Permutations / seed | 2000 / 42 |
| Minimum trades to test | 20 (below: `p_value = null`, never a survivor) |
| Minimum effective sample | 20 (`n_effective`, the conservative minimum of AR(1) and overlap corrections) |
| In-sample / out-of-sample split | 0.70 / 0.30 of the research window |
| Holdout fraction | 0.15 of the most recent bars, sealed |
| Discovery | allowed |
| Validation | allowed |
| Final holdout | sealed |
| Holdout reuse | prohibited |
| Parameter farming | prohibited |
| Post-result α changes | prohibited |
| Candidate-family expansion after inspection | prohibited |
| Promotion | requires multiplicity survival AND sealed confirmation AND forward evidence |
| Live trading | disabled |

## Rules that make the settings real

* **Holdout reuse is prohibited.** A confirmation run on the sealed slice is recorded once per
  (family, dataset) in `data/research_holdout.json`, with the dataset and holdout hashes. A repeat is
  refused; `--force-holdout` exists only to replace a record known to be void. Once looked at, the
  holdout is evidence about the research process, not another dataset.
* **Post-result α changes are prohibited.** α is fixed at 0.10 for the family under 8.1. Moving it
  after seeing a p-value turns the correction into a selection device.
* **Candidate-family expansion after inspection is prohibited.** The 8.1 family is the 26 candidates
  recorded in `data/research_manifest.json` (`family_id` `fam_849cebfdd1a4`, hypothesis version 1).
  Adding a candidate after reading the result would mean the corrected family no longer describes the
  search that produced the survivors.
* **Parameter farming is prohibited.** Variants of one idea belong to the same family and raise its
  size, which raises the required p-value; sweeping `lookback` 20, 21, 22, … is not a new experiment.
* **Promotion requires all three gates**: multiplicity survival (BH), a sealed confirmation, and
  forward evidence in the attribution buckets.
* **`thresholds_lowered: false` stays visible** in `stats.attribution`, `stats.evidence` and the
  research status payload, so that in six months the system answers "why didn't we just lower the
  threshold?" from its own provenance instead of anyone's memory.

## Interpretation rule (permanent)

Say **"failed to reject the null at the multiplicity-adjusted threshold"**, never "the strategy
doesn't work". A p-value is computed *under* the null; it is not the probability that the null is
true. The `n_effective`/power diagnostics exist precisely because a failure to reject at this sample
size is a statement about the experiment, not about the market.

## Amendment procedure

1. Record the proposed change and the reason in a new `docs/research-contract-<version>.md`.
2. State which 8.1 value it replaces and why the previous one cannot stand.
3. Re-run the family under the new version and report the new numbers alongside the 8.1 report.
4. Never edit this document. 8.1 results stay readable as 8.1 results.

## 8.1 result (for the record)

Produced 2026-09-22T16:00:10Z on 20,083 research bars (2025-09-21 → 2026-07-29) of XAUUSDm M15 with
the sealed holdout excluded; report SHA-256 `ebeb83af31d1…`, dataset SHA-256 `3903c58b9d97…`.

26 candidates searched, 11 cleared the raw gate stack, **0 survived** Benjamini–Hochberg at α = 0.10
(and 0 under BY and Bonferroni). Every candidate's evidence, dependence diagnostics and power block is
in `docs/evidence/2026-09-22-phase8-1-result.md`. The holdout remains sealed with 0 confirmations.
