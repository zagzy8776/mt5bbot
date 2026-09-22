"""Research validation: walk-forward, Monte Carlo, parameter and cost sensitivity."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from mt5_platform.backtest.data import Bar
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.backtest.metrics import Metrics, compute_metrics
from mt5_platform.strategy.registry import create_strategy


@dataclass
class WalkForwardWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_metrics: Metrics
    test_metrics: Metrics


@dataclass
class WalkForwardReport:
    windows: list[WalkForwardWindow] = field(default_factory=list)
    profitable_windows: int = 0
    total_windows: int = 0
    mean_test_pf: float = 0.0
    mean_test_expectancy: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.profitable_windows / self.total_windows if self.total_windows else 0.0


def walk_forward(
    strategy_name: str,
    params: dict,
    bars: list[Bar],
    config: BacktestConfig,
    *,
    windows: int = 5,
    oos_ratio: float = 0.25,
) -> WalkForwardReport:
    """Anchored walk-forward: train on [0..i], test on [i..i+step]."""
    report = WalkForwardReport()
    if len(bars) < windows * 50:
        return report
    segment = len(bars) // windows
    for i in range(1, windows):
        train_end = segment * i
        test_end = min(train_end + int(segment * oos_ratio * 2), len(bars))
        if test_end - train_end < 20:
            continue
        train = bars[:train_end]
        test = bars[train_end:test_end]
        try:
            strat_train = create_strategy(strategy_name, **params)
            strat_test = create_strategy(strategy_name, **params)
        except (ValueError, TypeError):
            return report
        tm = compute_metrics(run_backtest(train, [strat_train], config))
        vem = compute_metrics(run_backtest(test, [strat_test], config))
        report.windows.append(WalkForwardWindow(
            train_start=bars[0].time.strftime("%Y-%m-%d"),
            train_end=bars[train_end - 1].time.strftime("%Y-%m-%d"),
            test_start=bars[train_end].time.strftime("%Y-%m-%d"),
            test_end=bars[test_end - 1].time.strftime("%Y-%m-%d"),
            train_metrics=tm, test_metrics=vem,
        ))
    if report.windows:
        report.total_windows = len(report.windows)
        report.profitable_windows = sum(
            1 for w in report.windows if w.test_metrics.net_profit > 0
        )
        pfs = [min(w.test_metrics.profit_factor, 10.0) for w in report.windows]
        report.mean_test_pf = sum(pfs) / len(pfs)
        report.mean_test_expectancy = sum(
            w.test_metrics.expectancy_money for w in report.windows
        ) / len(report.windows)
    return report


@dataclass
class MonteCarloReport:
    n_simulations: int = 0
    median_final_equity: float = 0.0
    p05_final_equity: float = 0.0
    p95_final_equity: float = 0.0
    median_max_drawdown_pct: float = 0.0
    p95_max_drawdown_pct: float = 0.0
    pct_simulations_profitable: float = 0.0
    worst_losing_streak: int = 0


def monte_carlo(
    strategy_name: str,
    params: dict,
    bars: list[Bar],
    config: BacktestConfig,
    *,
    n_simulations: int = 500,
    seed: int = 42,
) -> MonteCarloReport:
    """Bootstrap-resample trades to test equity/drawdown path sensitivity.

    NOTE: simple reshuffling would NOT change final equity (sum is commutative),
    so we sample WITH replacement (bootstrap). That varies trade count slightly
    as well as order — both are realistic (a different market would give a
    different draw of trades). This is a simulation, not a prediction.
    """
    try:
        strat = create_strategy(strategy_name, **params)
    except (ValueError, TypeError):
        return MonteCarloReport()
    result = run_backtest(bars, [strat], config)
    trades = result.trades
    report = MonteCarloReport(n_simulations=n_simulations)
    if len(trades) < 10:
        return report
    rng = random.Random(seed)
    pnls = [t.pnl for t in trades]
    n = len(pnls)
    start = config.starting_balance
    finals: list[float] = []
    dds: list[float] = []
    streaks: list[int] = []
    for _ in range(n_simulations):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        eq = start
        peak = start
        max_dd = 0.0
        streak = worst = 0
        for p in sample:
            eq += p
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)
            streak = streak + 1 if p <= 0 else 0
            worst = max(worst, streak)
        finals.append(eq)
        dds.append(max_dd)
        streaks.append(worst)
    finals.sort()
    dds.sort()
    report.median_final_equity = finals[len(finals) // 2]
    report.p05_final_equity = finals[int(len(finals) * 0.05)]
    report.p95_final_equity = finals[int(len(finals) * 0.95)]
    report.median_max_drawdown_pct = dds[len(dds) // 2]
    report.p95_max_drawdown_pct = dds[int(len(dds) * 0.95)]
    report.pct_simulations_profitable = sum(1 for f in finals if f > start) / len(finals)
    report.worst_losing_streak = max(streaks)
    return report


@dataclass
class SensitivityReport:
    spread_multipliers: list[float] = field(default_factory=list)
    spread_net_profits: list[float] = field(default_factory=list)
    spread_pass_count: int = 0
    param_variations: list[dict] = field(default_factory=list)
    param_net_profits: list[float] = field(default_factory=list)
    param_positive_count: int = 0

    @property
    def spread_stable(self) -> bool:
        """Profitable at the configured spread (1x) AND at 2x that spread.

        Checking specific levels is stronger than "any 2 of 5" — a strategy that
        only survives cheap-spread scenarios is not robust to real conditions.
        """
        by_mult = dict(zip(self.spread_multipliers, self.spread_net_profits, strict=False))
        if 1.0 not in by_mult or 2.0 not in by_mult:
            return False
        return by_mult[1.0] > 0 and by_mult[2.0] > 0

    @property
    def param_stable(self) -> bool:
        """Majority of nearby parameters remain profitable."""
        if not self.param_net_profits:
            return False
        return self.param_positive_count / len(self.param_net_profits) >= 0.6


def cost_sensitivity(
    strategy_name: str,
    params: dict,
    bars: list[Bar],
    config: BacktestConfig,
    *,
    multipliers: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0),
) -> SensitivityReport:
    """Re-run with spread+slippage scaled by each multiplier."""
    report = SensitivityReport()
    for mult in multipliers:
        cfg = BacktestConfig(
            symbol=config.symbol, spec=config.spec,
            starting_balance=config.starting_balance, risk_pct=config.risk_pct,
            max_volume=config.max_volume, max_positions=config.max_positions,
            max_exposure_pct=config.max_exposure_pct,
            max_daily_loss_pct=config.max_daily_loss_pct,
            max_drawdown_pct=config.max_drawdown_pct,
            default_spread_price=config.default_spread_price * mult,
            slippage_price=config.slippage_price * mult,
            commission_per_lot=config.commission_per_lot,
        )
        try:
            strat = create_strategy(strategy_name, **params)
        except (ValueError, TypeError):
            continue
        m = compute_metrics(run_backtest(bars, [strat], cfg))
        report.spread_multipliers.append(mult)
        report.spread_net_profits.append(m.net_profit)
        if m.net_profit > 0:
            report.spread_pass_count += 1
    return report


def parameter_perturbation(
    strategy_name: str,
    params: dict,
    bars: list[Bar],
    config: BacktestConfig,
    *,
    variations: list[dict] | None = None,
) -> SensitivityReport:
    """Re-run with nearby parameter values to test stability."""
    report = SensitivityReport()
    if not variations:
        variations = _default_variations(params)
    for var in variations:
        try:
            strat = create_strategy(strategy_name, **var)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
        m = compute_metrics(run_backtest(bars, [strat], config))
        report.param_variations.append(var)
        report.param_net_profits.append(m.net_profit)
        if m.net_profit > 0:
            report.param_positive_count += 1
    return report


def _default_variations(params: dict) -> list[dict]:
    """Generate +/-20% variations of each numeric parameter."""
    out: list[dict] = []
    for key, value in params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for factor in (0.8, 1.2):
            v = value * factor
            if isinstance(value, int):
                v = max(1, int(round(v)))
            if v == value:
                continue
            out.append({**params, key: v})
    return out or [params]
