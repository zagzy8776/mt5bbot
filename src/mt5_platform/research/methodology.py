"""The statistical contract behind the research gates, written down and tested.

Everything the pipeline claims rests on the test in ``multiplicity.py``. This module states the
contract, measures what can be measured, and reports what cannot be measured as an explicit
limitation instead of an assumption.

Contract
--------
H0 (null)   : the candidate's expected per-trade R is <= 0 (no positive edge).
Statistic   : the mean of per-trade R over the evaluation window.
Null draws  : random sign flips of the observed |R| values (the sign of a trade is exchangeable
              under H0; the magnitudes are held fixed).
Sidedness   : two-sided -- a systematically losing candidate is also distinguishable from chance,
              and that is information about the hypothesis worth having.

Interpretation rules that go with it
------------------------------------
* Failing to reject H0 is **not** evidence that there is no edge. It means this data, at this sample
  size, could not distinguish the candidate from chance. Power is reported next to every p-value for
  exactly that reason.
* The sign-flip null assumes trades are exchangeable. Overlapping trades, serial dependence and
  clustered entries break that assumption in different ways, so the diagnostics here are reported
  alongside the p-value rather than silently ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import NormalDist
from typing import Any

H0 = "expected per-trade R <= 0 (no positive edge)"
STATISTIC = "mean per-trade R over the evaluation window"
NULL_DRAWS = "random sign flips of the observed |R| values (signs exchangeable under H0)"
SIDEDNESS = "two-sided"

DEFAULT_SIGMA_R = 1.0  # R is normalized by the stop distance, so its dispersion is ~1R by design
AUTOCORR_FLAG = 2.0  # |rho1| above 2/sqrt(n) is flagged as detectable dependence
FANO_FLAG = 2.0  # variance/mean of daily trade counts; > 2 means clustered entries


@dataclass(frozen=True)
class Assumption:
    """One assumption of the test, and how the pipeline deals with it."""

    id: str
    statement: str
    treatment: str
    testable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "treatment": self.treatment,
            "testable": self.testable,
        }


ASSUMPTIONS: tuple[Assumption, ...] = (
    Assumption(
        id="exchangeability",
        statement="the sign of a trade's R is exchangeable under H0",
        treatment="measured: overlap, clustering and serial dependence are reported per candidate",
    ),
    Assumption(
        id="serial_dependence",
        statement="residuals are not serially dependent (rho1 ~ 0)",
        treatment="measured: lag-1 autocorrelation and a runs test; n_eff_ar1 reported",
    ),
    Assumption(
        id="non_overlapping_trades",
        statement="trades do not overlap in time",
        treatment="measured: overlap fraction and mean concurrency; n_eff_overlap reported",
    ),
    Assumption(
        id="independent_outcomes",
        statement="outcomes are not clustered in a way that amplifies the same market move",
        treatment="measured: trades-per-day Fano factor",
    ),
    Assumption(
        id="costs_included",
        statement="spread, slippage, commission are already embedded in each trade's R",
        treatment="enforced: the backtest applies configured costs; cost sensitivity is a gate",
    ),
    Assumption(
        id="iid_permutation_draws",
        statement="permutation draws are independent and the p-value is uniform under H0",
        treatment="not directly testable here: size is calibrated on synthetic noise by test",
        testable=False,
    ),
)


def test_description(
    *, permutations: int, seed: int, min_trades: int, two_sided: bool = True
) -> dict[str, Any]:
    """Machine-readable description of the test, for the report and the manifest."""
    return {
        "h0": H0,
        "statistic": STATISTIC,
        "null_draws": NULL_DRAWS,
        "sidedness": SIDEDNESS if two_sided else "one-sided",
        "permutations": permutations,
        "seed": seed,
        "min_trades": min_trades,
        "interpretation": (
            "failing to reject H0 is not evidence of absence: see the power block for the smallest "
            "edge this sample could detect at the multiplicity-adjusted threshold"
        ),
        "assumptions": [assumption.to_dict() for assumption in ASSUMPTIONS],
    }


@dataclass
class DependenceDiagnostics:
    """Raw sample size vs how much independent information it really carries."""

    n_raw: int = 0
    n_used: int = 0
    overlap_pairs: int = 0
    overlap_fraction: float = 0.0
    max_concurrency: int = 0
    mean_concurrency: float = 1.0
    trades_per_day_max: int = 0
    trades_per_day_mean: float = 0.0
    fano_factor: float = 0.0
    autocorr_lag1: float = 0.0
    runs_z: float | None = None
    n_eff_ar1: float = 0.0
    n_eff_overlap: float = 0.0
    n_effective: float = 0.0
    flags: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_raw": self.n_raw,
            "n_used": self.n_used,
            "overlap_pairs": self.overlap_pairs,
            "overlap_fraction": self.overlap_fraction,
            "max_concurrency": self.max_concurrency,
            "mean_concurrency": self.mean_concurrency,
            "trades_per_day_max": self.trades_per_day_max,
            "trades_per_day_mean": self.trades_per_day_mean,
            "fano_factor": self.fano_factor,
            "autocorr_lag1": self.autocorr_lag1,
            "runs_z": self.runs_z,
            "n_eff_ar1": self.n_eff_ar1,
            "n_eff_overlap": self.n_eff_overlap,
            "n_effective": self.n_effective,
            "flags": list(self.flags),
            "note": self.note,
        }


def autocorr_lag1(values: Sequence[float]) -> float:
    """Lag-1 autocorrelation (0.0 when undefined)."""
    if len(values) < 3:
        return 0.0
    head, tail = list(values[:-1]), list(values[1:])
    mean_head = sum(head) / len(head)
    mean_tail = sum(tail) / len(tail)
    numerator = sum((a - mean_head) * (b - mean_tail) for a, b in zip(head, tail, strict=False))
    denom_head = sum((a - mean_head) ** 2 for a in head) ** 0.5
    denom_tail = sum((b - mean_tail) ** 2 for b in tail) ** 0.5
    if denom_head <= 0 or denom_tail <= 0:
        return 0.0
    return numerator / (denom_head * denom_tail)


def runs_z(values: Sequence[float]) -> float | None:
    """Wald-Wolfowitz runs test on the sign sequence (None when there is not enough data).

    A strongly negative z means the signs alternate less than chance would predict, i.e. outcomes
    arrive in streaks — the pattern that makes a raw trade count overstate the evidence.
    """
    signs = [1 if value > 0 else -1 for value in values if value != 0]
    n = len(signs)
    if n < 20:
        return None
    positives = sum(1 for sign in signs if sign > 0)
    negatives = n - positives
    if positives == 0 or negatives == 0:
        return None
    runs = 1 + sum(1 for a, b in zip(signs, signs[1:], strict=False) if a != b)
    expected = 2.0 * positives * negatives / n + 1.0
    variance = (2.0 * positives * negatives * (2.0 * positives * negatives - n)) / (
        n * n * (n - 1)
    )
    if variance <= 0:
        return None
    return (runs - expected) / variance**0.5


def dependence_diagnostics(
    trades: Iterable[tuple[datetime, datetime, float]],
    *,
    min_trades: int = 20,
) -> DependenceDiagnostics:
    """Diagnostics over ``(entry_time, exit_time, r_multiple)`` triples, in chronological order.

    Everything here is an explicit approximation and is labelled as one: ``n_eff_ar1`` assumes an
    AR(1) covariance and ``n_eff_overlap`` divides by mean concurrency. The purpose is to stop raw
    trade counts from being read as independent observations — it is not a dependence-aware test.
    """
    rows = sorted(trades, key=lambda row: row[0])
    result = DependenceDiagnostics(n_raw=len(rows), n_used=len(rows))
    result.note = (
        "n_effective is the conservative minimum of an AR(1) approximation and an overlap "
        "approximation. The permutation p-value reported alongside does NOT account for dependence "
        "yet: treat it as optimistic when any dependence flag is set."
    )
    if len(rows) < 2:
        result.n_eff_ar1 = result.n_eff_overlap = result.n_effective = float(result.n_raw)
        return result

    values = [row[2] for row in rows]
    result.autocorr_lag1 = autocorr_lag1(values)
    result.runs_z = runs_z(values)

    # Single sweep over entries: trades are sorted by entry, so every interval still open when the
    # next one starts is exactly one overlapping pair, and the open count is the concurrency.
    intervals = [(row[0], row[1]) for row in rows]
    open_closes: list[datetime] = []
    concurrency_sum = 0
    max_concurrency = 0
    for start, end in intervals:
        open_closes = [close for close in open_closes if close > start]
        result.overlap_pairs += len(open_closes)
        open_closes.append(end)
        concurrency_sum += len(open_closes)
        max_concurrency = max(max_concurrency, len(open_closes))
    result.max_concurrency = max_concurrency
    result.mean_concurrency = concurrency_sum / len(intervals) if intervals else 1.0
    result.overlap_fraction = (
        2.0 * result.overlap_pairs / (result.n_raw * (result.n_raw - 1))
        if result.n_raw > 1
        else 0.0
    )

    per_day: dict[date, int] = {}
    for start, _end, _r in rows:
        per_day[start.date()] = per_day.get(start.date(), 0) + 1
    counts = list(per_day.values())
    mean_per_day = sum(counts) / len(counts) if counts else 0.0
    result.trades_per_day_max = max(counts) if counts else 0
    result.trades_per_day_mean = mean_per_day
    if counts and mean_per_day > 0:
        variance = sum((count - mean_per_day) ** 2 for count in counts) / len(counts)
        result.fano_factor = variance / mean_per_day

    rho = result.autocorr_lag1
    ar1 = result.n_raw * (1.0 - rho) / (1.0 + rho) if rho > -1.0 else 1.0
    result.n_eff_ar1 = max(1.0, min(float(result.n_raw), ar1))
    result.n_eff_overlap = max(
        1.0, min(float(result.n_raw), result.n_raw / max(result.mean_concurrency, 1e-9))
    )
    result.n_effective = max(1.0, min(result.n_eff_ar1, result.n_eff_overlap))

    threshold = AUTOCORR_FLAG / max(result.n_raw, 1) ** 0.5
    if abs(rho) > threshold:
        result.flags.append(f"serial_dependence:rho1={rho:+.3f}>|{threshold:.3f}|")
    if result.runs_z is not None and abs(result.runs_z) > 2.0:
        result.flags.append(f"runs_test_z={result.runs_z:+.2f}")
    if result.overlap_pairs > 0:
        result.flags.append(
            f"overlapping_trades:pairs={result.overlap_pairs},"
            f"mean_concurrency={result.mean_concurrency:.2f}"
        )
    if result.fano_factor > FANO_FLAG:
        result.flags.append(f"clustered_entries:fano={result.fano_factor:.2f}")
    if result.n_used >= min_trades and result.n_effective < min_trades:
        result.flags.append(f"effective_sample_below_min:{result.n_effective:.1f}<{min_trades}")
    return result


@dataclass(frozen=True)
class PowerAnalysis:
    """What this sample could have detected — reported next to every p-value."""

    n_effective: float
    alpha_rank1: float
    z_critical: float
    sigma_r: float
    smallest_detectable_edge_r: float
    raw_trades_for_that_edge: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_effective": self.n_effective,
            "alpha_rank1": self.alpha_rank1,
            "z_critical": self.z_critical,
            "sigma_r_assumed": self.sigma_r,
            "smallest_detectable_edge_r": self.smallest_detectable_edge_r,
            "raw_trades_for_that_edge": self.raw_trades_for_that_edge,
            "note": (
                "Approximation: null SD of the mean is sigma_r/sqrt(n_effective) with "
                "sigma_r ~ 1R because R is normalized by the stop distance. Independent of the "
                "observed result, so it says what the experiment COULD have found."
            ),
        }


def z_critical(alpha: float) -> float:
    """Two-sided critical z for ``alpha`` (alpha is the smallest-rank threshold, e.g. alpha/m)."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    return NormalDist().inv_cdf(1.0 - alpha / 2.0)


def power_analysis(
    *,
    n_effective: float,
    alpha_rank1: float,
    sigma_r: float = DEFAULT_SIGMA_R,
) -> PowerAnalysis:
    """Smallest mean-R edge this sample could distinguish from chance at ``alpha_rank1``."""
    z = z_critical(alpha_rank1)
    n = max(float(n_effective), 1.0)
    smallest = z * sigma_r / n**0.5
    return PowerAnalysis(
        n_effective=n,
        alpha_rank1=alpha_rank1,
        z_critical=round(z, 4),
        sigma_r=sigma_r,
        smallest_detectable_edge_r=round(smallest, 4),
        raw_trades_for_that_edge=round((z * sigma_r / smallest) ** 2, 1) if smallest > 0 else 0.0,
    )


def trades_needed_for_edge(
    edge_r: float, *, alpha_rank1: float, sigma_r: float = DEFAULT_SIGMA_R
) -> float:
    """Approximate independent trades required to detect ``edge_r`` at ``alpha_rank1``."""
    if edge_r <= 0:
        raise ValueError("edge_r must be > 0")
    z = z_critical(alpha_rank1)
    return round((z * sigma_r / edge_r) ** 2, 1)


__all__ = [
    "ASSUMPTIONS",
    "AUTOCORR_FLAG",
    "DEFAULT_SIGMA_R",
    "FANO_FLAG",
    "H0",
    "NULL_DRAWS",
    "SIDEDNESS",
    "STATISTIC",
    "Assumption",
    "DependenceDiagnostics",
    "PowerAnalysis",
    "autocorr_lag1",
    "dependence_diagnostics",
    "power_analysis",
    "runs_z",
    "test_description",
    "trades_needed_for_edge",
    "z_critical",
]
