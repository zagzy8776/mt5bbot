"""Attribution: measuring which strategy/regime actually earns, without inventing a verdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.common.enums import (
    ExitCause,
    OrderSide,
    OutcomeSource,
    OutcomeStatus,
    RegimeLabel,
)
from mt5_platform.historical.attribution import (
    GROUPINGS,
    coverage_summary,
    outcome_attribution,
)
from mt5_platform.historical.models import HistoricalOutcome, SetupFeatures

T0 = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def _outcome(
    *,
    index: int,
    strategy: str = "breakout",
    regime: RegimeLabel | None = RegimeLabel.TRENDING,
    session: str = "london",
    pnl: float = 10.0,
    source: OutcomeSource = OutcomeSource.AUTONOMOUS,
    direction: OrderSide = OrderSide.BUY,
    exit_cause: ExitCause = ExitCause.TAKE_PROFIT,
    stop_loss: float = 2495.0,
    features: bool = True,
    status: OutcomeStatus = OutcomeStatus.CLOSED,
    entry: float = 2500.0,
    exit_price: float = 2505.0,
    return_pct: float | None = None,
) -> HistoricalOutcome:
    return HistoricalOutcome(
        instrument="XAUUSDm",
        direction=direction,
        timestamp=T0 + timedelta(hours=index),
        entry=entry,
        exit_price=exit_price,
        stop_loss=stop_loss,
        realized_pnl=pnl,
        return_pct=float(return_pct or 0.0),
        realized_pnl_pct=return_pct,
        status=status,
        source=source,
        exit_cause=exit_cause,
        features=(
            SetupFeatures(
                instrument="XAUUSDm",
                timestamp=T0 + timedelta(hours=index),
                strategy=strategy,
                regime=regime,
                session=session,
            )
            if features
            else None
        ),
    )


def _series(count: int, *, strategy: str = "breakout", pnl: float = 10.0, **kwargs) -> list:
    return [
        _outcome(index=i, strategy=strategy, pnl=pnl if i % 4 else -5.0, **kwargs)
        for i in range(count)
    ]


# ------------------------------------------------------------------------ grouping


def test_attribution_groups_by_strategy_and_orders_by_sample_size() -> None:
    outcomes = _series(30, strategy="breakout") + _series(12, strategy="ema_adx_trend", pnl=5.0)
    buckets = outcome_attribution(outcomes, group_by="strategy")

    assert [b.key for b in buckets] == ["breakout", "ema_adx_trend"]
    assert [b.sample_size for b in buckets] == [30, 12]
    assert sum(b.share for b in buckets) == pytest.approx(1.0)
    assert buckets[0].share > buckets[1].share


def test_attribution_grades_with_the_shared_thresholds() -> None:
    outcomes = (
        _series(30, strategy="strong")
        + _series(12, strategy="weak")
        + _series(3, strategy="tiny")
    )
    buckets = {b.key: b for b in outcome_attribution(outcomes, group_by="strategy")}

    assert buckets["strong"].evidence_quality == "moderate"
    assert buckets["strong"].verdict == "measured"
    assert buckets["weak"].evidence_quality == "weak"
    assert buckets["tiny"].evidence_quality == "insufficient"
    assert buckets["tiny"].verdict == "insufficient_evidence"
    for bucket in buckets.values():
        assert bucket.to_dict()["thresholds_lowered"] is False
        assert bucket.min_sample_weak == 10
        assert bucket.min_sample_moderate == 30
        assert bucket.min_sample_strong == 100


def test_attribution_computes_expectancy_r_and_pnl() -> None:
    outcomes = [
        # R is measured against the ORIGINAL stop distance (5.0 here)
        _outcome(  # +1.0R
            index=0, pnl=10.0, entry=2500.0, exit_price=2505.0, stop_loss=2495.0, return_pct=1.0
        ),
        _outcome(  # -1.0R
            index=1, pnl=-5.0, entry=2500.0, exit_price=2495.0, stop_loss=2495.0, return_pct=-0.5
        ),
        _outcome(  # +2.0R
            index=2, pnl=20.0, entry=2500.0, exit_price=2510.0, stop_loss=2495.0, return_pct=2.0
        ),
    ]
    bucket = outcome_attribution(outcomes, group_by="strategy")[0]
    assert bucket.sample_size == 3
    assert bucket.total_pnl == pytest.approx(25.0)
    assert bucket.expectancy_money == pytest.approx(25.0 / 3)
    assert bucket.expectancy_pct == pytest.approx((1.0 - 0.5 + 2.0) / 3)  # shared OutcomeStats rule
    assert bucket.r_samples == 3
    assert bucket.expectancy_r == pytest.approx((1.0 - 1.0 + 2.0) / 3)
    assert bucket.wins == 2 and bucket.losses == 1


def test_attribution_surfaces_unattributed_and_unlabelled_buckets() -> None:
    outcomes = [
        _outcome(index=0, strategy="", features=False),  # external trade, no setup recorded
        _outcome(index=1, strategy="", regime=None),
        _outcome(index=2, strategy="breakout", regime=None, session=""),
    ]
    by_strategy = {b.key: b for b in outcome_attribution(outcomes, group_by="strategy")}
    # a missing setup and an empty strategy name both surface as "unattributed"
    assert by_strategy["unattributed"].sample_size == 2
    assert by_strategy["breakout"].sample_size == 1

    by_regime = {b.key: b for b in outcome_attribution(outcomes, group_by="regime")}
    assert by_regime["unlabelled"].sample_size == 3

    by_session = {b.key: b for b in outcome_attribution(outcomes, group_by="session")}
    assert by_session["unlabelled"].sample_size == 2 and by_session["london"].sample_size == 1


def test_attribution_groups_by_regime_side_and_exit_cause() -> None:
    outcomes = _series(12, regime=RegimeLabel.TRENDING) + _series(
        12, regime=RegimeLabel.RANGING, direction=OrderSide.SELL, exit_cause=ExitCause.STOP_LOSS
    )
    by_regime = {b.key: b for b in outcome_attribution(outcomes, group_by="regime")}
    assert set(by_regime) == {"trending", "ranging"}
    assert by_regime["ranging"].wins + by_regime["ranging"].losses == 12

    by_side = {b.key: b for b in outcome_attribution(outcomes, group_by="side")}
    assert set(by_side) == {"buy", "sell"}

    by_cause = {b.key: b for b in outcome_attribution(outcomes, group_by="exit_cause")}
    assert by_cause["stop_loss"].sample_size == 12
    assert by_cause["take_profit"].sample_size == 12


def test_attribution_rejects_unknown_grouping() -> None:
    with pytest.raises(ValueError, match="unknown grouping"):
        outcome_attribution(_series(3), group_by="vibes")
    assert "strategy" in GROUPINGS and "regime" in GROUPINGS


# ----------------------------------------------------------- data-quality guards


def test_only_closed_outcomes_are_counted() -> None:
    open_outcome = _outcome(index=0, status=OutcomeStatus.OPEN, exit_price=None)
    buckets = outcome_attribution([open_outcome, *_series(12)], group_by="strategy")
    assert sum(bucket.sample_size for bucket in buckets) == 12


def test_backtest_contamination_is_flagged() -> None:
    mixed = [
        *_series(12, strategy="backtesty", source=OutcomeSource.BACKTEST),
        *_series(12, strategy="breakout"),
    ]
    buckets = {b.key: b for b in outcome_attribution(mixed, group_by="strategy")}
    assert buckets["backtesty"].contaminated is True
    assert buckets["backtesty"].sources == {"backtest": 12}
    assert buckets["breakout"].contaminated is False
    assert buckets["breakout"].sources == {"autonomous": 12}


def test_coverage_summary_separates_measured_from_unmeasured() -> None:
    outcomes = _series(30, strategy="ema_adx_trend") + _series(4, strategy="mtf_trend")
    summary = coverage_summary(outcomes)
    assert summary["closed_trades"] == 34
    assert summary["strategies_recorded"] == 2
    assert summary["measured"] == 1
    assert summary["unmeasured"] == 1
    assert summary["unmeasured_keys"] == ["mtf_trend"]
    assert summary["thresholds_lowered"] is False


def test_coverage_summary_reports_unattributed_trades() -> None:
    outcomes = _series(12, strategy="breakout") + _series(5, strategy="", features=False)
    summary = coverage_summary(outcomes)
    assert summary["unattributed_trades"] == 5
    assert summary["strategies_recorded"] == 1


def test_empty_record_is_explicitly_empty() -> None:
    assert outcome_attribution([]) == []
    summary = coverage_summary([])
    assert summary["closed_trades"] == 0 and summary["measured"] == 0
    assert summary["thresholds_lowered"] is False


def test_runtime_snapshot_exposes_attribution() -> None:
    """The control plane must be able to answer "which bucket is earning?" without more tooling."""
    import inspect

    from mt5_platform.runtime.service import BotControlService

    source = inspect.getsource(BotControlService.attribution_snapshot)
    assert "outcome_attribution" in source and "coverage_summary" in source
    assert 'stats["attribution"]' in inspect.getsource(BotControlService.snapshot.fget)
