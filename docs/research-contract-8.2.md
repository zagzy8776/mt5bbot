# RESEARCH CONTRACT 8.2 — gold "scalp-shaped" family

Status: **PRE-REGISTERED**. Written *before* the run, with no candidate's result seen. It defines a new
family under its own size; contract **8.1 remains frozen and untouched** (see
`docs/research-contract-8.1.md`, amendment procedure: a change needs a new version, a new document,
a new run and a new report). This document replaces no 8.1 value.

## Why a new version at all

The 8.1 family (`fam_849cebfdd1a4`, 26 candidates) asked whether *existing* strategies have an edge on
gold M15 and answered: 11 raw survivors, **0 after Benjamini–Hochberg** (best p = 0.078 `SMA 10/50`
against a rank-1 threshold of 0.003846).

This contract asks a *different* question — whether higher-frequency, tighter-stop logic has an edge —
which makes it a new hypothesis family with its own honest size, not a re-run of 8.1.

## Frozen settings for 8.2

| setting | value |
| --- | --- |
| Family ID | `fam_gold_scalp_m15` |
| Hypothesis version | `2` |
| **Candidate count (declared before the run)** | **12** (3 strategies × 4 parameter variants) |
| Instrument / timeframe | XAUUSDm / **M15** (the only timeframe with validated broker history) |
| Primary correction | Benjamini–Hochberg, α = 0.10 (unchanged from 8.1) |
| Sensitivity (never the verdict) | Benjamini–Yekutieli (α=0.10), Bonferroni (α=0.05) |
| Primary test | two-sided sign-flip permutation on OOS per-trade R, 2000 permutations, seed 42 |
| Minimum trades to test | 20 (below: `p_value = null`, never a survivor) |
| Minimum effective sample | 20 (`n_effective`) |
| In-sample / out-of-sample | 0.70 / 0.30 |
| Holdout | 0.15 of the most recent bars, **sealed**, fresh for 8.2 |
| Gate stack | unchanged (OOS + walk-forward + Monte Carlo + spread 1×/2× + parameter perturbation) |
| Session filter | **none** — pre-registered on the strength of the cost audit below |
| Live trading | disabled |

## Pre-registered cost model (the audit that shaped this family)

Measured with `scripts/gold_target.py --audit-csv data/xauusd_m15.csv` on 23,627 bars, **before** any
strategy result was seen. Spread is effectively **flat across the trading day**:

* 22 of 24 hours: mean **223–228 points**, median **240**.
* 21:00 UTC looks cheapest (mean 185.6, median 160) but covers only **332 bars** (~1/3 of the hours'
  sample) because the daily break truncates it; 22:00 UTC is the **most expensive** hour (median 260).
* Therefore **no low-spread window exists** with an adequate sample, and no session filter is
  pre-registered. Choosing one after reading results would be a selection device.

Cost ladder (spread + 0.05 slippage as a fraction of R):

| stop | price distance | cost | share of a 0.10R edge |
| --- | --- | --- | --- |
| 0.15% | 6.49 | 0.0478R | 48% |
| 0.20% | 8.65 | 0.0358R | 36% |
| **0.30%** | **12.97** | **0.0239R** | **24%** |
| **0.50%** | **21.62** | **0.0143R** | **14%** |
| 1.00% | 43.24 | 0.0072R | 7% |

Consequence, pre-registered: stops are restricted to **0.30% / 0.50%** and targets to **≥ 1R**. A
sub-0.15% "true scalp" stop surrenders ~half the edge to spread and is excluded by construction — not
because it was tested and failed, but because the arithmetic rules it out.

## The 12 candidates (declared before the run)

| # | candidate | strategy | stop | target | other |
| --- | --- | --- | --- | --- | --- |
| 1 | Scalp MB 10 | `scalp_micro_breakout` | 0.50% | 1.0R | lookback 10, ATR buffer 0.25 |
| 2 | Scalp MB 20 | `scalp_micro_breakout` | 0.50% | 1.0R | lookback 20, ATR buffer 0.25 |
| 3 | Scalp MB 20 b50 | `scalp_micro_breakout` | 0.50% | 1.0R | lookback 20, ATR buffer 0.5 |
| 4 | Scalp MB 40 tight | `scalp_micro_breakout` | 0.30% | 1.0R | lookback 40, ATR buffer 0.5 |
| 5 | Scalp REV 20 z1.0 | `scalp_vwap_reversion` | 0.50% | 1.0R | band window 20, 1.0 deviation |
| 6 | Scalp REV 20 z1.5 | `scalp_vwap_reversion` | 0.50% | 1.0R | band window 20, 1.5 deviation |
| 7 | Scalp REV 40 z1.5 | `scalp_vwap_reversion` | 0.30% | 1.0R | band window 40, 1.5 deviation |
| 8 | Scalp REV 40 z2.0 | `scalp_vwap_reversion` | 0.30% | 1.5R | band window 40, 2.0 deviation |
| 9 | Scalp MOM 07 | `scalp_session_momentum` | 0.50% | 1.0R | session 07:00–09:00 UTC |
| 10 | Scalp MOM 13 | `scalp_session_momentum` | 0.50% | 1.0R | session 13:00–15:00 UTC |
| 11 | Scalp MOM 07 13 | `scalp_session_momentum` | 0.50% | 1.5R | sessions 07:00–09:00 and 13:00–15:00 |
| 12 | Scalp MOM 15 | `scalp_session_momentum` | 0.30% | 1.0R | session 15:00–17:00 UTC |

Session hours were chosen from the audit (cheap-ness rank within hours that carry a full sample and
real liquidity), never from a strategy result. **Both directions** are allowed in all 12: no
long-only or short-only restriction is pre-registered, because no evidence supports one.

## Rules that make these settings real

* **No candidate may be added or removed after a result is read.** The family is 12. Trimming it to
  loosen α/m recreates the selection problem the correction exists to prevent (pinned by
  `tests/test_research_contract.py`).
* **Parameter farming is prohibited.** The 12 above are the search; `lookback` 21, 22, … is not a new
  experiment.
* **Post-result α changes are prohibited.**
* **Holdout reuse is prohibited.** The 8.1 holdout (3,544 bars, 0 confirmations) is **not** reused and
  **not** unsealed. A confirmation run on 8.2's holdout is recorded once per (family, dataset).
* **Promotion requires all three gates**: BH survival, sealed confirmation, forward evidence.
* **Interpretation rule (from 8.1, permanent):** a failure is reported as *"failed to reject the null
  at the multiplicity-adjusted threshold"*, never as "the strategy is bad". Power and `n_effective`
  are reported so the statement is about the experiment, not the market.

## What this contract does NOT claim

It does not claim scalping works, that cost can be beaten by speed, or that any high win rate is
available. σ_R on gold M15 implied by the 8.1 p-values is ≈**1.34**, so the smallest detectable edge
is ≈**0.31R** — larger than the 0.10R edge a $20/day, 4-trades-per-day plan assumed — and a 0.05R edge
needs ≈**5,990** independent trades to distinguish from noise. The most likely verdict is another
"0 survivors", and this document exists so that verdict is measured rather than assumed.

## Live scalping is explicitly out of scope

Nothing in 8.2 authorises a live change: no strategy is promoted, no runtime parameter is touched, no
gate is lowered, and the demo runtime keeps its current configuration. A survivor would only become
*eligible* for review through `scripts/promote_candidate.py`.

## Amendment procedure

As 8.1: record the change and reason in a new `docs/research-contract-<version>.md`, state which value
it replaces and why, re-run, report alongside. Never edit this document after results exist.

## 8.2 result

Recorded after the run in `docs/evidence/2026-09-22-phase8-2-gold-scalp-result.md`.
