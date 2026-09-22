"""Gold target arithmetic and cost audit: what $X/day requires, and what it risks."""

from __future__ import annotations

import pytest

from mt5_platform.research.targets import (
    DEFAULT_SIGMA_R,
    GOLD_CONTRACT_SIZE,
    cost_per_trade_r,
    cost_table,
    lots_for_risk,
    plan_gold_target,
    risk_per_trade,
    spread_by_hour,
    stop_distance_price,
    trades_needed_for_significance,
)

PRICE = 4324.0


# ------------------------------------------------------------------- position arithmetic


def test_gold_contract_arithmetic() -> None:
    assert GOLD_CONTRACT_SIZE == 100.0
    assert stop_distance_price(PRICE, 0.5) == pytest.approx(21.62)
    # 0.02 lots x 21.62 price x $100/lot = $43.24 at risk
    assert risk_per_trade(PRICE, 0.5, 0.02) == pytest.approx(43.24, abs=0.01)
    assert lots_for_risk(PRICE, 0.5, 43.24) == pytest.approx(0.02, abs=1e-6)
    assert lots_for_risk(PRICE, 0.5, 50.0) == pytest.approx(0.0231, abs=1e-4)
    with pytest.raises(ValueError):
        stop_distance_price(0.0, 0.5)
    with pytest.raises(ValueError):
        stop_distance_price(PRICE, 0.0)
    with pytest.raises(ValueError):
        risk_per_trade(PRICE, 0.5, -1.0)


def test_cost_in_r_shrinks_as_the_stop_widens() -> None:
    wide = cost_per_trade_r(0.26, stop_distance_price(PRICE, 0.5), 0.05)
    tight = cost_per_trade_r(0.26, stop_distance_price(PRICE, 0.15), 0.05)
    assert wide == pytest.approx(0.31 / 21.62, rel=1e-6)
    assert tight > wide, "a tighter stop pays more cost per unit of risk"
    assert tight / wide == pytest.approx(0.5 / 0.15, rel=1e-3)
    with pytest.raises(ValueError):
        cost_per_trade_r(0.26, 0.0)


# ------------------------------------------------------------------------- the target


def test_twenty_dollars_a_day_on_gold_needs_a_modest_size() -> None:
    plan = plan_gold_target(
        target_daily=20.0,
        trades_per_day=4.0,
        edge_r=0.10,
        price=PRICE,
        stop_pct=0.5,
        spread_price=0.26,
        slippage_price=0.05,
        balance=10_000.0,
    )
    assert plan.cost_r == pytest.approx(0.01434, abs=1e-4)
    assert plan.net_edge_r == pytest.approx(0.08566, abs=1e-4)
    assert plan.lots == pytest.approx(0.027, abs=0.001)
    assert plan.risk_per_trade == pytest.approx(58.37, abs=0.5)
    assert plan.expected_daily == pytest.approx(20.0, abs=0.5)
    assert plan.risk_pct_of_balance < 1.0
    assert plan.feasible is True
    assert plan.daily_risk == pytest.approx(233.5, abs=2.0)
    # ~2k independent trades before a 0.086R edge is separable from noise
    assert 1800 < plan.trades_for_significance < 2300
    assert plan.days_to_significance > 400


def test_a_target_the_edge_cannot_cover_is_infeasible_at_any_size() -> None:
    plan = plan_gold_target(
        target_daily=20.0,
        trades_per_day=4.0,
        edge_r=0.01,  # below the cost hurdle
        price=PRICE,
        balance=10_000.0,
    )
    assert plan.net_edge_r < 0
    assert plan.feasible is False
    assert plan.lots == 0.0
    assert any("no size is profitable" in warning for warning in plan.warnings)


def test_small_account_is_flagged_rather_than_silently_sized_up() -> None:
    plan = plan_gold_target(
        target_daily=20.0,
        trades_per_day=4.0,
        edge_r=0.10,
        price=PRICE,
        balance=1_000.0,
        max_risk_pct_per_trade=1.0,
    )
    assert plan.lots > 0
    assert plan.risk_pct_of_balance > 5.0, "$20/day on $1k needs ~5.8% risk per trade"
    assert plan.feasible is False
    assert any("exceeds the" in warning for warning in plan.warnings)
    assert plan.to_dict()["feasible"] is False


def test_more_frequency_needs_less_size_and_fewer_days() -> None:
    slow = plan_gold_target(
        target_daily=20.0, trades_per_day=2.0, edge_r=0.10, price=PRICE, balance=10_000.0
    )
    fast = plan_gold_target(
        target_daily=20.0, trades_per_day=8.0, edge_r=0.10, price=PRICE, balance=10_000.0
    )
    assert fast.lots < slow.lots
    assert fast.days_to_significance < slow.days_to_significance
    with pytest.raises(ValueError):
        plan_gold_target(target_daily=0.0, trades_per_day=4.0, edge_r=0.1, price=PRICE)
    with pytest.raises(ValueError):
        plan_gold_target(target_daily=20.0, trades_per_day=0.0, edge_r=0.1, price=PRICE)


def test_significance_requirement_scales_as_expected() -> None:
    small = trades_needed_for_significance(0.05)
    large = trades_needed_for_significance(0.10)
    assert small == pytest.approx(large * 4.0, rel=1e-6), "halving the edge quadruples the sample"
    assert DEFAULT_SIGMA_R == 1.34
    with pytest.raises(ValueError):
        trades_needed_for_significance(0.0)


# --------------------------------------------------------------------- spread audit


def test_spread_by_hour_buckets_and_orders() -> None:
    rows = [(13, 160.0), (13, 200.0), (2, 300.0), (13, 180.0), (7, 140.0)]
    buckets = spread_by_hour(rows)
    assert [bucket.label for bucket in buckets] == ["02:00", "07:00", "13:00"]
    asia, london, ny = buckets
    assert asia.bars == 1 and asia.mean_points == 300.0
    assert london.mean_points == 140.0
    assert ny.bars == 3
    assert ny.mean_points == pytest.approx(180.0)
    assert ny.median_points == 180.0
    assert ny.max_points == 200.0
    assert ny.p90_points == 200.0


def test_spread_audit_handles_empty_input() -> None:
    assert spread_by_hour([]) == []


def test_target_plan_is_labelled_observational_only() -> None:
    """The guardrail that keeps a dollar target from becoming a trading requirement.

    Causal order is edge -> validation -> forward evidence -> risk budget -> target as observation.
    A target must never be an input to selection, sizing or frequency.
    """
    plan = plan_gold_target(
        target_daily=20.0, trades_per_day=4.0, edge_r=0.10, price=PRICE, balance=10_000.0
    )
    payload = plan.to_dict()
    assert payload["observational_only"] is True
    assert "never an input to trade selection" in payload["note"]
    # the infeasible path carries the same label: a target cannot be rescued by more size
    hopeless = plan_gold_target(
        target_daily=20.0, trades_per_day=4.0, edge_r=0.01, price=PRICE, balance=10_000.0
    )
    assert hopeless.to_dict()["observational_only"] is True


def test_cost_table_quantifies_the_hurdle() -> None:
    rows = cost_table((0.2, 0.5), price=PRICE, spread_price=0.26, slippage_price=0.05)
    assert len(rows) == 2
    assert rows[1]["cost_r"] < rows[0]["cost_r"], "wider stops dilute the same spread"
    for row in rows:
        assert row["cost_r"] > 0
        assert row["cost_pct_of_0.10r_edge"] > 0
        assert row["risk_per_0.01_lot"] > 0
