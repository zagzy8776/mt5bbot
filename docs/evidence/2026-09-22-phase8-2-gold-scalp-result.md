# 2026-09-22 — contract 8.2 result: the gold "scalp-shaped" family (Path B)

Contract: `docs/research-contract-8.2.md` (**pre-registered**, written before the run). Family
`fam_gold_scalp_m15`, hypothesis version 2, 12 candidates declared in advance.
Run: `python -m mt5_platform.research.runner --family-id fam_gold_scalp_m15 --hypothesis-version 2`
Report `data/research_report_8.2.json`, manifest `data/research_manifest_8.2.json`.
(Family-isolated manifest: `data/research_manifest.json` is the frozen 8.1 artefact and was not
modified, so this run could not silently shrink its own multiplicity correction.)

## Verdict

**0 of 12 scalp candidates survived Benjamini–Hochberg at α = 0.10.** In fact **0 of 12 cleared the
raw gate stack** — so the family was already empty before multiplicity was applied, and the BH
threshold never had to do the work. Sensitivity corrections agree (BY 0, Bonferroni 0).

Said precisely, per the permanent interpretation rule: this is a **failure to reject the null at the
multiplicity-adjusted threshold**, not evidence that no edge exists.

## The 12 candidates (OOS = out-of-sample, E = expectancy in account currency)

| candidate | IS trades | OOS trades | OOS PF | OOS E | p | n_eff | raw gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Scalp REV 40 z1.5 | 1058 | 410 | 0.79 | **-22.22** | 0.0475 | 351.7 | fail |
| Scalp MB 20 b50 | 382 | 136 | 1.16 | +22.16 | 0.4038 | 120.9 | fail |
| Scalp REV 20 z1.5 | 513 | 337 | 0.92 | -12.44 | 0.4393 | 246.9 | fail |
| Scalp REV 40 z2.0 | 514 | 306 | 0.91 | -10.07 | 0.5397 | 244.5 | fail |
| Scalp MOM 15 | 383 | 139 | 1.13 | +13.06 | 0.5427 | 99.1 | fail |
| Scalp MB 20 | 468 | 179 | 1.09 | +13.10 | 0.5817 | 155.5 | fail |
| Scalp MOM 07 | 287 | 120 | 1.08 | +11.74 | 0.6922 | 82.3 | fail |
| Scalp MOM 07 13 | 525 | 210 | 1.07 | +10.69 | 0.6942 | 132.0 | fail |
| Scalp MB 10 | 576 | 225 | 1.03 | +4.24 | 0.8416 | 178.9 | fail |
| Scalp MOM 13 | 365 | 149 | 0.97 | -3.74 | 0.8421 | 106.2 | fail |
| Scalp MB 40 tight | 290 | 113 | 1.05 | +4.80 | 0.8541 | 112.0 | fail |
| Scalp REV 20 z1.0 | 504 | 356 | 0.99 | -1.32 | 0.9450 | 252.0 | fail |

For contrast, the same run's 8.1 candidates (re-tested inside this 38-candidate family, so their
p-values are *stricter* than in the 8.1 report): the rank-1 BH threshold with m = 38 is 0.00263 and
even the smallest p-value in the whole family (0.0475, `Scalp REV 40 z1.5`) is ~18× above it.

## What the numbers actually say

1. **The spread arithmetic predicted this.** 0.30–0.50% stops kept cost to 24% / 14% of a 0.10R edge —
   that part of the pre-registration worked — but the family's *gross* edge was never established, so
   cost was never the binding constraint. Cost analysis rules strategies out; it does not create them.
2. **The one candidate with a small p-value is a loser, not a winner.** `Scalp REV 40 z1.5` has OOS
   PF 0.79 and expectancy **-22.22 per trade** at the smallest p-value of the family. A "significant"
   result in the wrong direction is exactly what a two-sided test is supposed to surface, and the raw
   gates (which require OOS PF ≥ 0.9 and E > 0) correctly refused it.
3. **Frequency did not buy significance.** The scalp family traded far more than 8.1's candidates
   (up to 576 in-sample, 410 OOS) yet produced worse p-values. More trades with a smaller true edge
   is not more evidence — which is what `n_effective` exists to expose (82–352 independent units).
4. **Power is the honest ceiling.** Smallest detectable edge on this sample ≈ **0.2249R**
   (recomputed with σ_R from the data, not the 1.0 that 8.1 assumed). Any claimed edge below ~0.22R
   could not have been distinguished from noise here, so "no survivor" is a statement about this
   experiment's resolution as much as about the strategies.

## What this changes (nothing, operationally)

* **No promotion.** No candidate is eligible: the bridge requires BH survival explicitly.
* **8.1 stays frozen and untouched.** Its 26-candidate family, its report and its sealed holdout are
  unchanged; the 8.1 holdout was **not** reused or unsealed.
* **The 8.2 holdout remains sealed** — 0 confirmations recorded, because a confirmation run is only
  meaningful for a survivor and there are none.
* **Live trading untouched**: no strategy promoted, no runtime parameter changed, no gate lowered,
  demo-only. The scalp families exist in the registry as *research candidates*
  (`scalp_micro_breakout`, `scalp_vwap_reversion`, `scalp_session_momentum`) and are not in
  `STRATEGIES`.

## Follow-on options (each needs its own pre-registration)

1. **More data, not more variants.** The detection floor (~0.22R) is set by sample size and σ_R. A
   deeper history (M5/M1 via broker or Dukascopy) is the only change that raises resolution without
   inventing hypotheses — and note `fetch_mt5` currently needs `symbol_select` before
   `copy_rates_from_pos` or it returns 0 bars.
2. **A different instrument or a lower-cost account.** The audit shows gold M15 spread is flat ~240
   points all day; nothing in this repo can fix that. Cost is the one input where a real improvement
   is available outside the research engine.
3. **Do not re-open 8.2 by adding candidates.** That is the prohibited move: it raises m, requires a
   new contract version, and recreates the selection problem.
