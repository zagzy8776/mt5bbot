# Gold-only feasibility: what $20/day requires, and what it risks

Date: 2026-09-22
Scope: XAUUSDm only, as instructed. Nothing here changes a gate, a size or a setting.

## What is verified from the data on disk

| fact | value | source |
| --- | --- | --- |
| Gold history on disk | 23,627 M15 bars, 2025-09-21 22:00 → 2026-09-21 20:45 UTC | `data/xauusd_m15.csv` |
| Spread at the start of that file | **160 points** (0.16 price) | first data row |
| Spread at the end of that file | **240–260 points** (0.24–0.26 price) | last rows |
| Live spread now | **260 points** (bid 4325.005 / ask 4325.265) | `/api/v1/market/quote` |
| Cost charged by the 8.1 research | flat 0.26 spread + 0.05 slippage | `data/research_manifest.json` |
| Gold contract | 100 oz/lot → **$1 price = $100/lot** | instrument spec |

Two consequences worth stating plainly:

1. **The research's cost assumption was conservative, not flattering.** It charged the *current worst*
   spread (260 pts) across the whole year, including the period when the broker's book showed 160 pts.
   So the 8.1 candidates were not helped by optimistic costs.
2. **Trading the cheaper hours is real but small.** The file's own history says gold can be had at
   160 pts instead of 260; the by-hour audit (below) will quantify how much of the day that covers.

## The cost hurdle in R (the number an edge must clear)

`cost_R = (spread + slippage) / stop_distance`, with spread 0.26, slippage 0.05, price 4,324:

| stop | stop distance | risk per 0.01 lot | cost_R | share of a 0.10R edge |
| --- | --- | --- | --- | --- |
| 0.15% | 6.49 | $6.49 | 0.0477R | 48% |
| 0.20% | 8.65 | $8.65 | 0.0358R | 36% |
| 0.30% | 12.97 | $12.97 | 0.0239R | 24% |
| 0.50% | 21.62 | $21.62 | **0.0143R** | 14% |
| 1.00% | 43.24 | $43.24 | 0.0072R | 7% |

**Correction to my earlier framing:** I had called cost engineering "the cheapest real edge". For gold
at the 0.5% stops these strategies actually use, cost is **~1.4% of R** — it did *not* kill the
candidates. Tighter stops raise it sharply (0.2% stops pay 3.6% of R, and if the true edge were 0.05R
that is 72% of it), so cost becomes decisive only for high-frequency, tight-stop designs. What killed
the 8.1 candidates was **significance**, not the spread. That distinction matters for where to spend
the next week of work.

## Sizing model: what a $20/day target *would* require (observational, not a requirement)

The causal order is fixed and not negotiable:

```
research discovers edge -> edge survives validation -> forward demo establishes evidence
-> risk budget determines size -> a dollar target becomes an observation
```

Reading it backwards (`$20/day -> choose lots -> force 4 trades/day -> find enough trades`) would make
the target an input to trade selection and directly contradict the research contract. Every payload
from `plan_gold_target` therefore carries `observational_only: true` and the note "a dollar target is
an OUTPUT of a validated edge and a risk budget; it is never an input to trade selection, sizing or
frequency".

With that understood, the table below says what a hypothetical edge would *imply* (edge is gross;
cost is subtracted; size only closes the arithmetic gap):

| gross edge | net edge after cost | lots implied by $20/day | risk/trade | % of $10k | independent trades needed | days at 4/day |
| --- | --- | --- | --- | --- | --- | --- |
| 0.05R | 0.0357R | 0.065 | $140 | **1.40%** | ~8,800 | ~2,200 |
| 0.10R | 0.0857R | **0.027** | $58 | 0.58% | ~2,040 | ~510 |
| 0.15R | 0.1357R | 0.017 | $37 | 0.37% | ~910 | ~228 |
| 0.20R | 0.1857R | 0.012 | $27 | 0.27% | ~460 | ~115 |

Two observations that follow (as consequences, not as goals):

* **If** a 0.10R edge is ever validated, the size needed for $20/day on gold is small — 0.027 lots
  (≈2.7 oz), $58 at risk, 0.58% of a $10,000 account. The existing live cap (0.02 lots) is already the
  right order of magnitude, which means sizing is *not* what stands between this bot and the target.
* **The edge and the sample are the whole problem.** At 0.05R the implied risk per trade breaches a
  1% cap, and even at 0.10R about 2,000 independent trades (~17 months at 4/day) are needed before the
  edge could be told apart from noise. On gold that makes *more history* the lever — not more size, and
  certainly not more trades to satisfy a daily number.

## The plan for gold, in order

1. **Audit the cost by hour** (`scripts/gold_target.py --audit-csv data/xauusd_m15.csv`). Purpose: to
   answer **"when is a strategy economically viable?"** — *not* "which hours are cheapest so we can
   manufacture profitability?". Liquidity costs vary with conditions, and the audit exists to make the
   cost treatment explicit. Whatever it finds, the **session rule gets pre-registered before any
   strategy result is inspected**; picking the cheapest hours after seeing results is the same
   selection problem the contract forbids.
2. **Get more gold history** — `python -m mt5_platform.backtest fetch-mt5 --symbol XAUUSDm
   --timeframe M15 --days N` uses `copy_rates_range` **with `symbol_select`** (the reason a raw
   `copy_rates_from_pos` probe returned 0 bars), and `fetch-dukascopy` exists as a deep-history path.
   More history is the only honest way to buy statistical power on one instrument.
3. **Pre-register the gold hypotheses** under a new contract version (8.2-gold). The family size is
   whatever the research question honestly implies — if it is six conceptual hypotheses, then m = 6; if
   it is twenty-six, then m = 26. **The threshold must never drive the family size**: handing 8.2 a
   trimmed list to obtain a looser α/m would recreate precisely the selection problem 8.1 was built to
   prevent. (For reference only, not as a design target: m = 6 gives a rank-1 threshold ≈ 0.0167 versus
   ≈ 0.00385 at m = 26.)
4. **Then** forward demo, at a size set by the risk budget, with a pre-registered stopping rule.

## Blocker at the time of writing

The VS Code terminal is wedged for this session (commands do not return; it started after an MT5
`initialize()` probe). The bot is unaffected — this is the shell, not the runtime, and nothing about
the runtime, schedules or lot sizes has been touched. Everything above is either read from files or
computed by hand from verified inputs. The by-hour audit, the pytest run and the Ruff check each need a
fresh PowerShell; until they run, `targets.py`, `scripts/gold_target.py` and `tests/test_gold_target.py`
stay **uncommitted and explicitly unverified**.

