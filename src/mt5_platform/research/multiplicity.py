"""Multiplicity control for strategy research.

Testing 25 candidates and keeping whoever looks best is how backtests lie. At a raw 5% false
positive rate you would expect roughly one of 25 pure-noise candidates to look "profitable"; with
several gates the search space is even bigger. Every candidate therefore gets a permutation p-value
on its out-of-sample per-trade returns, and the family is corrected (Benjamini-Hochberg by default)
before anything is called a survivor.

Two rules are deliberate:

* the test runs on **out-of-sample** trades, because in-sample results were used to pick the
  candidate in the first place;
* "not enough trades to test" is reported as ``p_value=None`` and can never be a survivor — absence
  of evidence is not evidence.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PERMUTATIONS = 2000
DEFAULT_SEED = 42
DEFAULT_MIN_TRADES = 20
DEFAULT_ALPHA = 0.10


@dataclass(frozen=True)
class PermutationTest:
    """Sign-flip permutation test of H0: the sign of each trade result is random."""

    n_trades: int
    n_permutations: int
    seed: int
    observed_mean: float
    p_value: float | None  # None when there is not enough evidence to test
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "n_permutations": self.n_permutations,
            "seed": self.seed,
            "observed_mean": self.observed_mean,
            "p_value": self.p_value,
            "reason": self.reason,
        }


def sign_flip_permutation(
    returns: Sequence[float],
    *,
    n_permutations: int = DEFAULT_PERMUTATIONS,
    seed: int = DEFAULT_SEED,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> PermutationTest:
    """Two-sided sign-flip permutation p-value for the mean of ``returns``.

    Deterministic for a given seed, so a report can be re-derived later. Add-one smoothing keeps the
    p-value strictly positive (finitely many permutations can never prove p = 0).
    """
    values = [float(r) for r in returns]
    n = len(values)
    observed = sum(values) / n if n else 0.0
    if n < min_trades:
        return PermutationTest(
            n_trades=n,
            n_permutations=0,
            seed=seed,
            observed_mean=observed,
            p_value=None,
            reason=f"insufficient_trades:{n}<{min_trades}",
        )
    magnitudes = [abs(v) for v in values]
    if all(m == 0.0 for m in magnitudes):
        # every trade was exactly flat: there is nothing to test and nothing to claim
        return PermutationTest(
            n_trades=n,
            n_permutations=0,
            seed=seed,
            observed_mean=observed,
            p_value=1.0,
            reason="all_trades_flat",
        )
    rng = random.Random(seed)
    threshold = abs(observed)
    extreme = 0
    for _ in range(n_permutations):
        total = 0.0
        for magnitude in magnitudes:
            total += magnitude if rng.random() < 0.5 else -magnitude
        if abs(total / n) >= threshold:
            extreme += 1
    return PermutationTest(
        n_trades=n,
        n_permutations=n_permutations,
        seed=seed,
        observed_mean=observed,
        p_value=min(1.0, (extreme + 1) / (n_permutations + 1)),
    )


@dataclass
class MultiplicityReport:
    """What the correction did to the family of candidates."""

    method: str = "benjamini-hochberg"
    alpha: float = DEFAULT_ALPHA
    tested: int = 0
    untestable: list[str] = field(default_factory=list)
    survivors: list[str] = field(default_factory=list)
    adjusted: dict[str, float] = field(default_factory=dict)
    raw_p_values: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "alpha": self.alpha,
            "tested": self.tested,
            "untestable": list(self.untestable),
            "survivors": list(self.survivors),
            "raw_p_values": dict(self.raw_p_values),
            "adjusted_p_values": dict(self.adjusted),
        }


def benjamini_hochberg(
    p_values: dict[str, float], *, alpha: float = DEFAULT_ALPHA
) -> MultiplicityReport:
    """Benjamini-Hochberg step-up: controls the expected proportion of false discoveries.

    Chosen over Bonferroni because this is a screening step: missing a real edge is expensive, and
    survivors still have to clear the walk-forward, Monte Carlo, cost and perturbation gates.
    """
    report = MultiplicityReport(method="benjamini-hochberg", alpha=alpha, tested=len(p_values))
    report.raw_p_values = {name: float(p) for name, p in p_values.items()}
    if not p_values:
        return report
    ordered = sorted(report.raw_p_values.items(), key=lambda item: item[1])
    total = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank in range(total, 0, -1):
        name, p = ordered[rank - 1]
        running = min(running, p * total / rank)
        adjusted[name] = min(1.0, running)
    report.adjusted = adjusted
    report.survivors = [name for name, _ in ordered if adjusted[name] <= alpha]
    return report


def bonferroni(p_values: dict[str, float], *, alpha: float = 0.05) -> MultiplicityReport:
    """Family-wise error control: the strictest of the two, kept for comparison in reports."""
    report = MultiplicityReport(method="bonferroni", alpha=alpha, tested=len(p_values))
    report.raw_p_values = {name: float(p) for name, p in p_values.items()}
    if not p_values:
        return report
    total = len(p_values)
    report.adjusted = {name: min(1.0, p * total) for name, p in report.raw_p_values.items()}
    report.survivors = sorted(
        (name for name, value in report.adjusted.items() if value <= alpha),
        key=lambda name: report.raw_p_values[name],
    )
    return report


def apply_multiplicity(
    p_values: dict[str, float | None],
    *,
    method: str = "benjamini-hochberg",
    alpha: float = DEFAULT_ALPHA,
) -> MultiplicityReport:
    """Correct a family of candidates. A candidate without a p-value can never survive."""
    testable = {name: float(p) for name, p in p_values.items() if p is not None}
    untestable = sorted(name for name, p in p_values.items() if p is None)
    chosen = bonferroni if method == "bonferroni" else benjamini_hochberg
    report = chosen(testable, alpha=alpha)
    report.untestable = untestable
    return report


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_MIN_TRADES",
    "DEFAULT_PERMUTATIONS",
    "MultiplicityReport",
    "PermutationTest",
    "apply_multiplicity",
    "benjamini_hochberg",
    "bonferroni",
    "sign_flip_permutation",
]
