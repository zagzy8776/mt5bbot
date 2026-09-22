"""Phase 8.1: the statistical contract, dependence diagnostics and power reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.research.methodology import (
    ASSUMPTIONS,
    H0,
    SIDEDNESS,
    STATISTIC,
    autocorr_lag1,
    dependence_diagnostics,
    power_analysis,
    runs_z,
    trades_needed_for_edge,
    z_critical,
)
from mt5_platform.research.methodology import test_description as describe_test

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _row(index: int, *, start_hour: float, duration_hours: float, r: float):
    start = T0 + timedelta(hours=start_hour)
    return (start, start + timedelta(hours=duration_hours), r)


def _spaced(count: int, *, r: float = 1.0, step_hours: float = 4.0, duration_hours: float = 1.0):
    """Non-overlapping, well-separated trades (the clean case)."""
    return [
        _row(index, start_hour=index * step_hours, duration_hours=duration_hours, r=r)
        for index in range(count)
    ]


# -------------------------------------------------------------------- the contract


def test_test_description_states_the_contract() -> None:
    described = describe_test(permutations=2000, seed=42, min_trades=20)
    assert described["h0"] == H0 and "R <= 0" in described["h0"]
    assert described["statistic"] == STATISTIC
    assert described["sidedness"] == SIDEDNESS
    assert described["permutations"] == 2000 and described["seed"] == 42
    assert "not evidence of absence" in described["interpretation"]
    ids = {entry["id"] for entry in described["assumptions"]}
    assert {"exchangeability", "serial_dependence", "non_overlapping_trades"} <= ids
    assert any(entry["testable"] is False for entry in described["assumptions"])
    assert len(ASSUMPTIONS) == len(described["assumptions"])


# ------------------------------------------------------------------- diagnostics


def test_clean_series_keeps_the_full_effective_sample() -> None:
    diagnostics = dependence_diagnostics(_spaced(60))
    assert diagnostics.n_raw == 60
    assert diagnostics.overlap_pairs == 0
    assert diagnostics.max_concurrency == 1
    assert diagnostics.n_effective == pytest.approx(60.0, abs=1e-6)
    assert diagnostics.flags == []


def test_overlapping_trades_reduce_the_effective_sample() -> None:
    # one trade per hour, each lasting two hours: about two are open at any moment
    rows = [_row(i, start_hour=i * 1.0, duration_hours=2.0, r=1.0) for i in range(40)]
    diagnostics = dependence_diagnostics(rows)
    assert diagnostics.overlap_pairs > 0
    assert diagnostics.mean_concurrency == pytest.approx(2.0, abs=0.1)
    assert diagnostics.n_effective < diagnostics.n_raw
    assert any(flag.startswith("overlapping_trades") for flag in diagnostics.flags)


def test_serial_dependence_is_flagged_by_both_measures() -> None:
    alternating = [
        _row(i, start_hour=i * 4.0, duration_hours=1.0, r=1.0 if i % 2 else -1.0)
        for i in range(60)
    ]
    diagnostics = dependence_diagnostics(alternating)
    assert diagnostics.autocorr_lag1 < -0.5
    assert diagnostics.runs_z is not None and diagnostics.runs_z > 2.0
    assert any(flag.startswith("serial_dependence") for flag in diagnostics.flags)
    # negative autocorrelation is informative, but we never claim MORE information than raw trades
    assert diagnostics.n_eff_ar1 == pytest.approx(float(diagnostics.n_raw))


def test_streaky_outcomes_are_flagged_by_the_runs_test() -> None:
    wins = _spaced(30, r=1.0)
    losses = [
        _row(i + 30, start_hour=(i + 30) * 4.0, duration_hours=1.0, r=-1.0) for i in range(30)
    ]
    diagnostics = dependence_diagnostics([*wins, *losses])
    assert diagnostics.runs_z is not None and diagnostics.runs_z < -2.0
    assert any(flag.startswith("runs_test_z") for flag in diagnostics.flags)
    assert diagnostics.autocorr_lag1 > 0.0
    assert diagnostics.n_eff_ar1 < diagnostics.n_raw  # positive dependence costs information


def test_clustered_entries_are_flagged() -> None:
    # 32 trades but only two active days, very unevenly loaded
    rows = [
        _row(i, start_hour=i * 0.1, duration_hours=0.05, r=1.0) for i in range(30)
    ] + [_row(i, start_hour=120.0 + i, duration_hours=0.05, r=1.0) for i in range(2)]
    diagnostics = dependence_diagnostics(rows)
    assert diagnostics.trades_per_day_max >= 30
    assert diagnostics.fano_factor > 2.0
    assert any(flag.startswith("clustered_entries") for flag in diagnostics.flags)


def test_effective_sample_below_minimum_is_flagged() -> None:
    rows = [_row(i, start_hour=i * 0.2, duration_hours=3.0, r=1.0) for i in range(30)]
    diagnostics = dependence_diagnostics(rows, min_trades=20)
    assert diagnostics.n_effective < 20
    assert any(flag.startswith("effective_sample_below_min") for flag in diagnostics.flags)


def test_diagnostics_are_degenerate_safe() -> None:
    empty = dependence_diagnostics([])
    assert empty.n_raw == 0 and empty.n_effective == 0.0
    single = dependence_diagnostics([_row(0, start_hour=0, duration_hours=1, r=1.0)])
    assert single.n_raw == 1 and single.n_effective == 1.0
    assert "does NOT account for dependence" in single.note
    assert autocorr_lag1([1.0, 2.0]) == 0.0
    assert runs_z([1.0, -1.0, 1.0]) is None  # too few samples to test


# -------------------------------------------------------------------------- power


def test_power_shrinks_with_more_effective_samples() -> None:
    small = power_analysis(n_effective=100, alpha_rank1=0.1 / 26)
    large = power_analysis(n_effective=400, alpha_rank1=0.1 / 26)
    assert large.smallest_detectable_edge_r < small.smallest_detectable_edge_r
    assert large.smallest_detectable_edge_r == pytest.approx(
        small.smallest_detectable_edge_r / 2.0, rel=0.02
    )
    assert small.alpha_rank1 == pytest.approx(0.1 / 26)


def test_power_reports_what_a_small_edge_would_need() -> None:
    needed = trades_needed_for_edge(0.05, alpha_rank1=0.1 / 26)
    assert 3000 < needed < 3600  # ~3.4k independent trades for a 0.05R edge at this threshold
    assert trades_needed_for_edge(0.05, alpha_rank1=0.1 / 5) < needed  # smaller search, lower bar
    assert z_critical(0.05) > z_critical(0.10)
    with pytest.raises(ValueError):
        trades_needed_for_edge(0.0, alpha_rank1=0.05)
    with pytest.raises(ValueError):
        z_critical(1.5)


def test_dependence_gate_blocks_validation_when_the_sample_does_not_support_it() -> None:
    from mt5_platform.research.runner import CandidateReport, apply_dependence_gate

    def candidate(name: str, n_effective: float) -> CandidateReport:
        return CandidateReport(
            name=name,
            params={},
            symbol="XAUUSDm",
            timeframe="M15",
            data_range=("a", "b"),
            oos_trades=50,
            validation_passed=True,
            dependence={"n_effective": n_effective},
        )

    thin = candidate("thin", 12.0)
    solid = candidate("solid", 220.0)
    flagged = apply_dependence_gate([thin, solid], min_trades=20)

    assert flagged == ["thin"]
    assert thin.validation_passed is False
    assert any(
        reason.startswith("insufficient_effective_sample") for reason in thin.rejection_reasons
    )
    assert solid.validation_passed is True and solid.rejection_reasons == []
