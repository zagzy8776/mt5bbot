"""Multiplicity control: the difference between "looks best of 25" and "is distinguishable"."""

from __future__ import annotations

import random

import pytest

from mt5_platform.research.multiplicity import (
    DEFAULT_ALPHA,
    apply_multiplicity,
    benjamini_hochberg,
    benjamini_yekutieli,
    bonferroni,
    sensitivity,
    sign_flip_permutation,
)

# ------------------------------------------------------------------ permutation test


def test_permutation_needs_enough_trades_to_say_anything() -> None:
    result = sign_flip_permutation([0.5, -0.4, 0.3], min_trades=20)
    assert result.p_value is None, "absence of evidence must not become evidence"
    assert result.reason.startswith("insufficient_trades")
    assert result.n_trades == 3


def test_permutation_flags_a_strong_edge() -> None:
    edge = [1.5] * 60 + [-1.0] * 20  # 75% winners: a real signal
    strong = sign_flip_permutation(edge, n_permutations=1000, seed=7)
    assert strong.p_value is not None and strong.p_value < 0.01
    assert strong.observed_mean > 0

    flat = sign_flip_permutation([0.0] * 50, n_permutations=100, seed=7)
    assert flat.p_value == 1.0 and flat.reason == "all_trades_flat"


def test_permutation_size_stays_near_the_nominal_level_on_noise() -> None:
    """A single noise series can look significant (that is why we correct the family).

    What must hold is the *rate*: on pure noise only a small fraction of candidates may look
    significant, or the p-values would be meaningless.
    """
    rng = random.Random(4242)
    rounds = 120
    flagged = 0
    for index in range(rounds):
        series = [rng.gauss(0.0, 1.0) for _ in range(80)]
        result = sign_flip_permutation(series, n_permutations=200, seed=index)
        assert result.p_value is not None and 0.0 < result.p_value <= 1.0
        if result.p_value <= 0.05:
            flagged += 1
    rate = flagged / rounds
    assert rate <= 0.15, f"the permutation test flags noise too often: {rate:.1%}"


def test_permutation_is_deterministic_and_bounded() -> None:
    returns = [0.8, -0.6, 1.1, -0.3, 0.5, -0.2, 0.9, -0.7] * 5
    first = sign_flip_permutation(returns, n_permutations=500, seed=123)
    second = sign_flip_permutation(returns, n_permutations=500, seed=123)
    different = sign_flip_permutation(returns, n_permutations=500, seed=999)
    assert first.p_value == second.p_value, "the same seed must reproduce the same p-value"
    assert first.p_value != different.p_value or first.p_value is None
    assert first.p_value is not None and 0.0 < first.p_value <= 1.0


def test_permutation_is_two_sided() -> None:
    losing = [-1.5] * 60 + [1.0] * 20  # consistently losing: also distinguishable from chance
    result = sign_flip_permutation(losing, n_permutations=1000, seed=3)
    assert result.p_value is not None and result.p_value < 0.01
    assert result.observed_mean < 0


# --------------------------------------------------------------------- corrections


def test_benjamini_hochberg_matches_the_standard_step_up() -> None:
    # classic teaching example: p = .001, .008, .039, .041, .042, .06 with m = 6, alpha = .05
    p_values = {
        "a": 0.001,
        "b": 0.008,
        "c": 0.039,
        "d": 0.041,
        "e": 0.042,
        "f": 0.060,
    }
    report = benjamini_hochberg(p_values, alpha=0.05)
    # largest k with p_(k) <= k/m*alpha is k=5 (0.042 <= 0.0417 is false, so k=5 would be wrong)…
    # the step-up rule keeps a, b, c and d (p_4 = 0.041 <= 4/6*0.05 = 0.0333? no) — assert on the
    # adjusted values, which are what the report publishes.
    assert report.adjusted["a"] == pytest.approx(0.006, abs=1e-9)
    assert report.adjusted["b"] == pytest.approx(0.024, abs=1e-9)
    assert report.adjusted["f"] == pytest.approx(0.06, abs=1e-9)
    assert all(value <= 1.0 for value in report.adjusted.values())
    # adjusted p-values are monotone in the raw p-values
    ordered = sorted(p_values, key=lambda name: p_values[name])
    adjusted = [report.adjusted[name] for name in ordered]
    assert adjusted == sorted(adjusted)
    assert report.survivors == [name for name in ordered if report.adjusted[name] <= 0.05]


def test_benjamini_hochberg_is_never_stricter_than_bonferroni() -> None:
    p_values = {f"c{i}": value for i, value in enumerate([0.001, 0.01, 0.02, 0.2, 0.5])}
    bh = benjamini_hochberg(p_values, alpha=0.05)
    bonf = bonferroni(p_values, alpha=0.05)
    assert set(bonf.survivors) <= set(bh.survivors)
    for name in p_values:
        assert bh.adjusted[name] <= bonf.adjusted[name] + 1e-12


def test_a_candidate_without_a_p_value_can_never_survive() -> None:
    report = apply_multiplicity({"solid": 0.001, "untested": None}, alpha=0.05)
    assert report.untestable == ["untested"]
    assert "untested" not in report.survivors
    assert set(report.raw_p_values) == {"solid"}


def test_empty_family_is_handled() -> None:
    report = apply_multiplicity({}, alpha=0.05)
    assert report.tested == 0 and report.survivors == [] and report.adjusted == {}


def test_benjamini_yekutieli_is_a_labelled_sensitivity_not_a_replacement() -> None:
    p_values = {"a": 0.001, "b": 0.01, "c": 0.03, "d": 0.2, "e": 0.6}
    bh = benjamini_hochberg(p_values, alpha=0.05)
    by = benjamini_yekutieli(p_values, alpha=0.05)

    assert by.method == "benjamini-yekutieli" and bh.method == "benjamini-hochberg"
    harmonic = 1.0 + 0.5 + 1 / 3 + 0.25 + 0.2  # H_5
    assert by.adjusted["a"] == pytest.approx(0.001 * 5 * harmonic, rel=1e-9)
    for name in p_values:
        assert by.adjusted[name] >= bh.adjusted[name] - 1e-12
    assert set(by.survivors) <= set(bh.survivors)
    # leaving the primary method alone is the point: BH still answers here
    assert "a" in bh.survivors


def test_sensitivity_reports_all_three_corrections_over_the_same_family() -> None:
    reports = sensitivity({"a": 0.0001, "b": None, "c": 0.5}, alpha=0.10)
    assert set(reports) == {"benjamini-hochberg", "benjamini-yekutieli", "bonferroni"}
    for method, report in reports.items():
        assert report.raw_p_values == {"a": 0.0001, "c": 0.5}, method
        assert "b" not in report.adjusted, "an untestable candidate never enters a correction"


# ------------------------------------------------------- the reason the control exists


def test_best_of_many_noise_series_does_not_survive_the_correction() -> None:
    """Twenty-five pure-noise candidates WILL produce a small raw p-value; BH must reject it.

    This is the exact failure mode the research pipeline has to avoid: whichever of many candidates
    looks best is not evidence of anything.
    """
    rng = random.Random(20260922)
    family: dict[str, float | None] = {}
    for index in range(25):
        series = [rng.gauss(0.0, 1.0) for _ in range(120)]
        family[f"noise_{index:02d}"] = sign_flip_permutation(
            series, n_permutations=400, seed=1000 + index
        ).p_value

    testable = {name: p for name, p in family.items() if p is not None}
    naive = [name for name, p in testable.items() if p <= 0.05]
    corrected = apply_multiplicity(family, alpha=0.05)

    assert naive or len(testable) > 0, "the family must be testable for this test to mean anything"
    assert corrected.survivors == [], (
        f"noise survived the correction: {corrected.survivors} "
        f"(raw flags: {naive}, adjusted: {corrected.adjusted})"
    )
    assert corrected.tested == 25


def test_integration_pass_folds_the_verdict_into_validation() -> None:
    from mt5_platform.research.runner import CandidateReport, apply_multiplicity_pass

    strong = CandidateReport(
        name="strong",
        params={},
        symbol="XAUUSDm",
        timeframe="M15",
        data_range=("a", "b"),
        validation_passed=True,
        p_value=0.0005,
    )
    marginal = CandidateReport(
        name="marginal",
        params={},
        symbol="XAUUSDm",
        timeframe="M15",
        data_range=("a", "b"),
        validation_passed=True,
        p_value=0.04,  # would look fine on its own, not after correction
    )
    untestable = CandidateReport(
        name="untestable",
        params={},
        symbol="XAUUSDm",
        timeframe="M15",
        data_range=("a", "b"),
        validation_passed=True,
        p_value=None,
    )
    already_failed = CandidateReport(
        name="broken",
        params={},
        symbol="XAUUSDm",
        timeframe="M15",
        data_range=("a", "b"),
        validation_passed=False,
        rejection_reasons=["IS trades=3<30"],
        p_value=0.9,
    )

    report = apply_multiplicity_pass(
        [strong, marginal, untestable, already_failed], alpha=0.01
    )

    assert report.tested == 3
    assert report.untestable == ["untestable"]
    assert strong.multiplicity_survivor is True and strong.validation_passed is True
    for candidate in (marginal, untestable):
        assert candidate.multiplicity_survivor is False
        assert candidate.validation_passed is False
        assert any(
            reason.startswith("fails_multiplicity_control")
            for reason in candidate.rejection_reasons
        ), candidate.rejection_reasons
    assert untestable.p_value_adjusted is None
    assert "no_permitted_p_value" in untestable.rejection_reasons[-1]
    # an already-failed candidate keeps its own reasons and gains nothing
    assert already_failed.rejection_reasons == ["IS trades=3<30"]
    assert already_failed.multiplicity_survivor is False
    assert DEFAULT_ALPHA == 0.10
