"""Validation: walk-forward, bootstrap Monte Carlo, spread/param sensitivity."""

from __future__ import annotations

from mt5_platform.backtest.data import synthetic_bars
from mt5_platform.backtest.engine import BacktestConfig
from mt5_platform.common.instruments import InstrumentSpec
from mt5_platform.research.validation import (
    cost_sensitivity,
    monte_carlo,
    parameter_perturbation,
    walk_forward,
)


def _cfg() -> BacktestConfig:
    spec = InstrumentSpec("XAUUSDm", 100.0, 0.001, 0.1, volume_min=0.01, volume_step=0.01)
    return BacktestConfig(
        symbol="XAUUSDm",
        spec=spec,
        starting_balance=100_000.0,
        risk_pct=1.0,
        default_spread_price=0.26,
        slippage_price=0.05,
    )


def test_walk_forward_shape() -> None:
    bars = synthetic_bars(3000, seed=11)
    report = walk_forward(
        "sma_crossover",
        {"fast_period": 5, "slow_period": 20},
        bars,
        _cfg(),
        windows=3,
    )
    assert report.total_windows >= 1
    for w in report.windows:
        assert w.train_start and w.test_end
        assert w.test_metrics.n_trades >= 0
    assert 0.0 <= report.pass_rate <= 1.0


def test_monte_carlo_bootstrap_shape() -> None:
    bars = synthetic_bars(4000, seed=12)
    report = monte_carlo(
        "sma_crossover",
        {"fast_period": 5, "slow_period": 20},
        bars,
        _cfg(),
        n_simulations=50,
    )
    if report.n_simulations:
        assert 0.0 <= report.pct_simulations_profitable <= 1.0
        assert report.median_max_drawdown_pct >= 0.0
        assert report.p95_max_drawdown_pct >= report.median_max_drawdown_pct


def test_cost_sensitivity_monotonic_trend() -> None:
    bars = synthetic_bars(3000, seed=13)
    report = cost_sensitivity(
        "breakout",
        {"lookback": 20},
        bars,
        _cfg(),
        multipliers=(0.5, 1.0, 2.0, 3.0),
    )
    assert report.spread_multipliers == [0.5, 1.0, 2.0, 3.0]
    # Each trade has the same bars and sizing; higher modeled costs cannot add net profit.
    profits = report.spread_net_profits
    assert len(profits) == 4
    assert all(later <= earlier for earlier, later in zip(profits, profits[1:], strict=False))
    assert isinstance(report.spread_stable, bool)


def test_parameter_perturbation_bounds() -> None:
    bars = synthetic_bars(3000, seed=14)
    report = parameter_perturbation("breakout", {"lookback": 20}, bars, _cfg())
    assert report.param_variations  # at least one variation generated
    assert len(report.param_net_profits) == len(report.param_variations)
    assert isinstance(report.param_stable, bool)
