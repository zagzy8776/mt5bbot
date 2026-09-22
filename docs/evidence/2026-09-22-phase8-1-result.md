# Phase 8.1 result — the completed 26-candidate experiment

Date: 2026-09-22T16:00:10Z
Contract: `docs/research-contract-8.1.md` (frozen). Code: commit `1e381fd3` (verified unchanged since).
Dataset: `data/xauusd_m15.csv`, SHA-256 `3903c58b9d97…`. Report SHA-256 `ebeb83af31d1…`.
Family: `fam_849cebfdd1a4`, hypothesis version 1, 26 candidates.

## Outcome

| | |
| --- | --- |
| Candidates searched | 26 |
| Cleared the raw gate stack (OOS + walk-forward + Monte Carlo + spread 1×/2× + parameter perturbation) | 11 |
| Survived Benjamini–Hochberg (α = 0.10) | **0** |
| Survived Benjamini–Yekutieli (α = 0.10) | 0 |
| Survived Bonferroni (α = 0.05) | 0 |
| Effective-sample rejections | 0 |
| Best raw p-value | 0.078 (`SMA 10/50`) |
| Rank-1 threshold at m = 26, α = 0.10 | 0.003846 |
| Verdict | `no_candidate_survived` |
| Promotable | 0 |

Research window: 20,083 bars (2025-09-21 → 2026-07-29). Sealed holdout: 3,544 bars
(2026-07-29 → 2026-09-21, hash `64e1377528d0…`), **confirmations on record: 0**.

Reading, in the words the contract requires: no candidate produced a result that **rejected the null
at the multiplicity-adjusted threshold**. That is a statement about this experiment's ability to
detect an edge in this problem space, not proof that the strategies have none.

## Candidate-by-candidate record

`oosT` OOS trades · `oosPF` OOS profit factor · `WF` walk-forward pass rate · `MC` Monte-Carlo
profitable paths · `spd`/`par` spread and parameter stability · `meanR` mean OOS R · `p` raw
sign-flip p · `n_eff/raw` effective vs raw OOS trades · `flg` dependence flags raised.

| candidate | oosT | oosPF | WF | MC | spd | par | meanR | p | n_eff/raw | flg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Donchian 20 | 246 | 1.17 | 100% | 95% | Y | Y | +0.105 | 0.257 | 184.5/246 | 3 |
| Donchian 30 | 205 | 1.18 | 75% | 92% | Y | Y | +0.108 | 0.298 | 166.1/205 | 1 |
| Donchian 50 | 167 | 1.24 | 50% | 96% | Y | Y | +0.151 | 0.186 | 157.6/167 | 1 |
| Donchian 100 | 128 | 0.96 | 50% | 94% | Y | Y | −0.026 | 0.855 | 109.9/128 | 0 |
| SMA 5/20 | 276 | 0.86 | 25% | 43% | – | Y | −0.098 | 0.275 | 197.9/276 | 2 |
| SMA 8/30 | 206 | 1.19 | 50% | 79% | Y | Y | +0.108 | 0.304 | 159.5/206 | 3 |
| SMA 10/50 | 129 | 1.43 | 50% | 85% | Y | Y | **+0.241** | **0.078** | 108.8/129 | 1 |
| SMA 5/50 | 152 | 1.21 | 50% | 76% | Y | Y | +0.119 | 0.330 | 121.6/152 | 1 |
| SMA 20/100 | 63 | 1.29 | 50% | 52% | Y | – | +0.177 | 0.339 | 46.3/63 | 1 |
| MeanRev w20 z2.0 | 260 | 0.94 | 50% | 5% | – | – | −0.038 | 0.669 | 206.1/260 | 3 |
| MeanRev w20 z1.5 | 330 | 1.05 | 25% | 4% | – | – | +0.037 | 0.625 | 240.9/330 | 3 |
| MeanRev w30 z2.5 | 148 | 0.91 | 25% | 6% | – | – | −0.067 | 0.575 | 130.4/148 | 3 |
| MeanRev w50 z2.0 | 194 | 0.84 | 50% | 4% | – | – | −0.120 | 0.228 | 173.4/194 | 1 |
| Momentum lb10 0.5% | 188 | 1.24 | 75% | 76% | Y | Y | +0.149 | 0.179 | 149.8/188 | 1 |
| Momentum lb20 0.5% | 175 | 1.26 | 50% | 94% | Y | Y | +0.161 | 0.157 | 144.5/175 | 3 |
| Momentum lb20 1.0% | 94 | 0.87 | 50% | 61% | Y | – | −0.092 | 0.504 | 76.1/94 | 1 |
| Momentum lb30 0.3% | 174 | 1.14 | 75% | 12% | – | Y | +0.074 | 0.485 | 132.8/174 | 3 |
| ATR 20 x2.0 | 262 | 0.98 | 0% | 4% | – | – | +0.014 | 0.862 | 255.2/262 | 1 |
| ATR 50 x1.5 | 125 | 0.90 | 0% | 36% | – | – | −0.077 | 0.533 | 125.0/125 | 0 |
| EMA 12/26 ADX20 | 267 | 1.11 | 75% | 98% | Y | Y | +0.064 | 0.474 | 210.3/267 | 2 |
| EMA 8/34 ADX25 | 242 | 1.08 | 50% | 85% | Y | Y | +0.049 | 0.588 | 196.5/242 | 1 |
| Bollinger 20 2.0 | 306 | 0.94 | 25% | 7% | – | – | −0.034 | 0.625 | 254.4/306 | 3 |
| RSI 14 EMA50 | 104 | 1.08 | 50% | 56% | – | Y | +0.060 | 0.676 | 98.3/104 | 1 |
| Session ORB 4 H7-16 | 158 | 1.04 | 50% | 63% | Y | Y | +0.017 | 0.851 | 146.8/158 | 1 |
| MTF H1 M15 20 | 217 | 1.18 | 50% | 77% | Y | Y | +0.108 | 0.274 | 164.6/217 | 3 |
| Structure 2/2 R2 | 469 | 0.92 | 50% | 75% | Y | Y | −0.092 | 0.171 | 382.5/469 | 1 |

Raw-gate survivors (11): Donchian 20, Donchian 30, Donchian 50, SMA 8/30, SMA 5/50,
Momentum lb10 0.5%, Momentum lb20 0.5%, EMA 12/26 ADX20, EMA 8/34 ADX25, Session ORB 4 H7-16,
MTF H1 M15 20 — every one of them failed only on multiplicity.

## What the record shows

1. **The ordinary stack is not a filter against searching.** 11 of 26 candidates passed it; with a
   nominal 5% error rate you would expect roughly one false positive in a family this size even if
   nothing had an edge. That gap is what the correction exists for.
2. **Raw counts overstate information.** `n_eff` sits 5–27% below the raw OOS count for the candidates
   that matter: Donchian 20 246 → 184.5, Donchian 30 205 → 166.1, MeanRev w20 z1.5 330 → 240.9,
   Structure 2/2 R2 469 → 382.5. Three-quarters of candidates raised at least one dependence flag
   (overlap, serial dependence, streaks or clustering); no candidate was rejected by the
   effective-sample gate, so the diagnostics did not change the verdict — they are now on record.
3. **The best result is ~20× from significance.** `SMA 10/50`: mean R +0.241, OOS PF 1.43, p = 0.078
   against a rank-1 threshold of 0.003846. Two-sidedness also flags a *losing* pattern at noise level
   (`Structure 2/2 R2`, mean R −0.092, p = 0.171) — the test behaves as documented.
4. **The power block's σ_R ≈ 1R assumption is optimistic for these families.** Inferring σ_R from the
   observed p-values (`σ ≈ mean_R · √n_eff / z`): Donchian 20 1.26, Donchian 30 1.34, Donchian 50 1.43,
   SMA 10/50 1.43, Momentum lb20 0.5% 1.37, Momentum lb10 1.36, SMA 8/30 1.33, SMA 5/50 1.34,
   SMA 20/100 1.26, MTF H1 M15 20 1.27 → **mean ≈ 1.34** against the assumed 1.00. Corrected:
   * detection floor at the median `n_eff` of 159.5: **≈0.31R, not 0.229R**;
   * independent trades needed for a 0.05R edge: **≈5,990, not 3,342**.
   The experiment is therefore *weaker* than the reported power block implies. This is an input to the
   next contract version — deliberately **not** patched inside 8.1.

## Decision

Path B applies: **zero survivors ⇒ do not search this problem space again on this data.** The next work
is the data expansion (multi-year history, additional instruments) with a small pre-registered
hypothesis family, under a new contract version that estimates σ_R from the data instead of assuming
it. The 15% holdout stays untouched (0 confirmations) and remains the sealed confirmation set for those
future candidates.

No candidate was enabled, promoted or configured. Live trading remains disabled; demo only.
