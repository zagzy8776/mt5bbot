"""Research runner: tests strategy candidates on real XAUUSDm M15 data."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mt5_platform.backtest.data import Bar, load_csv
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.backtest.metrics import compute_metrics
from mt5_platform.backtest.research import split_bars
from mt5_platform.common.instruments import InstrumentSpec
from mt5_platform.research.multiplicity import (
    DEFAULT_ALPHA,
    MultiplicityReport,
    apply_multiplicity,
    sign_flip_permutation,
)
from mt5_platform.research.validation import (
    cost_sensitivity,
    monte_carlo,
    parameter_perturbation,
    walk_forward,
)


@dataclass
class CandidateReport:
    name: str
    params: dict
    symbol: str
    timeframe: str
    data_range: tuple[str, str]
    strategy: str = ""  # factory name, so a promotion can rebuild the exact candidate
    is_trades: int = 0
    is_win_rate: float = 0.0
    is_profit_factor: float = 0.0
    is_expectancy: float = 0.0
    is_return_pct: float = 0.0
    is_max_drawdown_pct: float = 0.0
    oos_trades: int = 0
    oos_win_rate: float = 0.0
    oos_profit_factor: float = 0.0
    oos_expectancy: float = 0.0
    oos_return_pct: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    validation_passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    wf_pass_rate: float = 0.0
    wf_mean_pf: float = 0.0
    mc_pct_profitable: float = 0.0
    mc_median_dd_pct: float = 0.0
    mc_p95_dd_pct: float = 0.0
    spread_stable: bool = False
    param_stable: bool = False
    spread_net_profits: list[float] = field(default_factory=list)
    param_net_profits: list[float] = field(default_factory=list)
    # Multiplicity control (see research/multiplicity.py): a candidate is only a survivor after the
    # whole family is corrected, so selecting the best of N cannot masquerade as an edge.
    oos_mean_r: float = 0.0
    p_value: float | None = None  # sign-flip permutation on OOS per-trade R multiples
    p_value_adjusted: float | None = None
    multiplicity_survivor: bool | None = None
    multiplicity_method: str = ""
    n_permutations: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def run_candidate(
    name: str,
    strategy_name: str,
    params: dict,
    bars: list[Bar],
    config: BacktestConfig,
    oos_fraction: float = 0.3,
    permutations: int = 2000,
    seed: int = 42,
) -> CandidateReport:
    from mt5_platform.strategy.registry import create_strategy

    report = CandidateReport(
        name=name,
        strategy=strategy_name,
        params=params,
        symbol=config.symbol,
        timeframe="M15",
        data_range=(
            bars[0].time.strftime("%Y-%m-%d"),
            bars[-1].time.strftime("%Y-%m-%d"),
        ),
    )
    if len(bars) < 500:
        report.rejection_reasons.append(f"insufficient data: {len(bars)} bars")
        return report
    train, test = split_bars(bars, oos_fraction)
    try:
        strat = create_strategy(strategy_name, **params)
    except (ValueError, TypeError) as e:
        report.rejection_reasons.append(f"creation failed: {e}")
        return report
    is_m = compute_metrics(run_backtest(train, [strat], config))
    report.is_trades = is_m.n_trades
    report.is_win_rate = is_m.win_rate
    report.is_profit_factor = is_m.profit_factor
    report.is_expectancy = is_m.expectancy_money
    report.is_return_pct = is_m.return_pct
    report.is_max_drawdown_pct = is_m.max_drawdown_pct
    oos_result = run_backtest(test, [strat], config)
    oos_m = compute_metrics(oos_result)
    report.oos_trades = oos_m.n_trades
    report.oos_win_rate = oos_m.win_rate
    report.oos_profit_factor = oos_m.profit_factor
    report.oos_expectancy = oos_m.expectancy_money
    report.oos_return_pct = oos_m.return_pct
    report.oos_max_drawdown_pct = oos_m.max_drawdown_pct
    # Significance is measured on the out-of-sample trades only: in-sample results already picked
    # this candidate, so testing them would be circular.
    permutation = sign_flip_permutation(
        [t.r_multiple for t in oos_result.trades],
        n_permutations=permutations,
        seed=seed,
    )
    report.oos_mean_r = permutation.observed_mean
    report.p_value = permutation.p_value
    report.n_permutations = permutation.n_permutations
    reasons: list[str] = []
    if report.is_trades < 30:
        reasons.append(f"IS trades={report.is_trades}<30")
    if report.is_profit_factor < 1.0:
        reasons.append(f"IS PF={report.is_profit_factor:.2f}<1.0")
    if report.is_expectancy <= 0:
        reasons.append(f"IS E={report.is_expectancy:.2f}<=0")
    if report.is_max_drawdown_pct > 20.0:
        reasons.append(f"IS DD={report.is_max_drawdown_pct:.1f}%>20%")
    if report.oos_trades < 10:
        reasons.append(f"OOS trades={report.oos_trades}<10")
    if report.oos_profit_factor < 0.9:
        reasons.append(f"OOS PF={report.oos_profit_factor:.2f}<0.9")
    if report.oos_expectancy <= 0:
        reasons.append(f"OOS E={report.oos_expectancy:.2f}<=0")
    if report.oos_max_drawdown_pct > 25.0:
        reasons.append(f"OOS DD={report.oos_max_drawdown_pct:.1f}%>25%")

    # Walk-forward (anchored, 5 windows)
    wf = walk_forward(strategy_name, params, bars, config, windows=5)
    report.wf_pass_rate = wf.pass_rate
    report.wf_mean_pf = wf.mean_test_pf
    if wf.total_windows >= 3 and wf.pass_rate < 0.5:
        reasons.append(
            f"WF pass rate {wf.pass_rate:.0%} < 50%"
            f" ({wf.profitable_windows}/{wf.total_windows})"
        )

    # Monte Carlo trade reshuffle (bootstrap resample)
    mc = monte_carlo(strategy_name, params, bars, config, n_simulations=300)
    report.mc_pct_profitable = mc.pct_simulations_profitable
    report.mc_median_dd_pct = mc.median_max_drawdown_pct
    report.mc_p95_dd_pct = mc.p95_max_drawdown_pct
    if mc.n_simulations and mc.pct_simulations_profitable < 0.6:
        reasons.append(
            f"MC profitable only {mc.pct_simulations_profitable:.0%} of paths"
        )

    # Cost sensitivity: must survive 1x and 2x spread
    cs = cost_sensitivity(strategy_name, params, bars, config)
    report.spread_stable = cs.spread_stable
    report.spread_net_profits = cs.spread_net_profits
    if not cs.spread_stable:
        reasons.append(
            "spread unstable: profitable at "
            f"{cs.spread_pass_count}/{len(cs.spread_multipliers)} cost levels"
        )

    # Parameter perturbation
    pp = parameter_perturbation(strategy_name, params, bars, config)
    report.param_stable = pp.param_stable
    report.param_net_profits = pp.param_net_profits
    if pp.param_net_profits and not pp.param_stable:
        reasons.append(
            "param unstable: "
            f"{pp.param_positive_count}/{len(pp.param_net_profits)} nearby positive"
        )

    report.rejection_reasons = reasons
    report.validation_passed = len(reasons) == 0
    return report


def apply_multiplicity_pass(
    results: list[CandidateReport],
    *,
    method: str = "benjamini-hochberg",
    alpha: float = DEFAULT_ALPHA,
) -> MultiplicityReport:
    """Correct the family of candidates and fold the verdict into ``validation_passed``.

    A candidate that passed every other gate but is not a multiplicity survivor is marked as failed
    with an explicit reason, so no downstream reader can accidentally treat it as validated.
    """
    report = apply_multiplicity(
        {candidate.name: candidate.p_value for candidate in results},
        method=method,
        alpha=alpha,
    )
    survivors = set(report.survivors)
    for candidate in results:
        candidate.multiplicity_method = report.method
        candidate.p_value_adjusted = report.adjusted.get(candidate.name)
        candidate.multiplicity_survivor = candidate.name in survivors
        if candidate.validation_passed and candidate.name not in survivors:
            adjusted = candidate.p_value_adjusted
            detail = "no_permitted_p_value" if adjusted is None else f"p_adj={adjusted:.3f}"
            candidate.rejection_reasons = [
                *candidate.rejection_reasons,
                f"fails_multiplicity_control:{detail}:alpha={report.alpha}",
            ]
            candidate.validation_passed = False
    return report


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="research candidates on real bars (OOS + walk-forward + MC + costs + "
        "perturbation + multiplicity control)"
    )
    parser.add_argument("--csv", default=r"C:\mt5bbot\data\xauusd_m15.csv")
    parser.add_argument("--out", default=r"C:\mt5bbot\data\research_report.json")
    parser.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help="false-discovery rate for the multiplicity correction (default 0.10)",
    )
    parser.add_argument(
        "--method",
        default="benjamini-hochberg",
        choices=("benjamini-hochberg", "bonferroni"),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="run only candidates whose label contains this text (repeatable)",
    )
    parser.add_argument("--permutations", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found. Run fetch-mt5 first.")
        return 1
    report = load_csv(csv_path)
    bars = report.bars
    print(f"Loaded {len(bars)} bars: {bars[0].time} to {bars[-1].time}")
    spec = InstrumentSpec(
        "XAUUSDm", 100.0, 0.001, 0.1, volume_min=0.01, volume_step=0.01, digits=3
    )
    config = BacktestConfig(
        symbol="XAUUSDm",
        spec=spec,
        starting_balance=100_000.0,
        risk_pct=1.0,
        max_volume=0.10,
        default_spread_price=0.26,
        slippage_price=0.05,
        max_exposure_pct=300.0,
    )
    candidates = [
        # A. Trend following / breakout (Donchian-style)
        ("Donchian 20", "breakout", {"lookback": 20}),
        ("Donchian 30", "breakout", {"lookback": 30}),
        ("Donchian 50", "breakout", {"lookback": 50}),
        ("Donchian 100", "breakout", {"lookback": 100}),
        # B. SMA crossover (trend-following via MAs)
        ("SMA 5/20", "sma_crossover", {"fast_period": 5, "slow_period": 20}),
        ("SMA 8/30", "sma_crossover", {"fast_period": 8, "slow_period": 30}),
        ("SMA 10/50", "sma_crossover", {"fast_period": 10, "slow_period": 50}),
        ("SMA 5/50", "sma_crossover", {"fast_period": 5, "slow_period": 50}),
        ("SMA 20/100", "sma_crossover", {"fast_period": 20, "slow_period": 100}),
        # C. Mean reversion (z-score)
        ("MeanRev w20 z2.0", "mean_reversion", {"window": 20, "threshold": 2.0}),
        ("MeanRev w20 z1.5", "mean_reversion", {"window": 20, "threshold": 1.5}),
        ("MeanRev w30 z2.5", "mean_reversion", {"window": 30, "threshold": 2.5}),
        ("MeanRev w50 z2.0", "mean_reversion", {"window": 50, "threshold": 2.0}),
        # D. Momentum (ROC) - realistic thresholds for M15 gold
        ("Momentum lb10 0.5%", "momentum", {"lookback": 10, "threshold_pct": 0.5}),
        ("Momentum lb20 0.5%", "momentum", {"lookback": 20, "threshold_pct": 0.5}),
        ("Momentum lb20 1.0%", "momentum", {"lookback": 20, "threshold_pct": 1.0}),
        ("Momentum lb30 0.3%", "momentum", {"lookback": 30, "threshold_pct": 0.3}),
        # E. Phase 7 families: volatility, trend strength, bands, pullbacks, sessions, MTF,
        #    structure. Same runner, same gates — these arrive as candidates, not as decisions.
        ("ATR 20 x2.0", "atr_breakout", {"lookback": 20, "atr_period": 14, "atr_buffer": 0.25}),
        ("ATR 50 x1.5", "atr_breakout", {"lookback": 50, "atr_period": 14, "atr_buffer": 0.5}),
        ("EMA 12/26 ADX20", "ema_adx_trend", {"fast_period": 12, "slow_period": 26}),
        ("EMA 8/34 ADX25", "ema_adx_trend",
         {"fast_period": 8, "slow_period": 34, "adx_threshold": 25.0}),
        ("Bollinger 20 2.0", "bollinger_reversion", {"period": 20, "deviations": 2.0}),
        ("RSI 14 EMA50", "rsi_ema_pullback", {"trend_period": 50, "rsi_period": 14}),
        ("Session ORB 4 H7-16", "session_breakout",
         {"session_start_hour": 7, "opening_range_bars": 4}),
        ("MTF H1 M15 20", "mtf_trend", {"htf_factor": 4, "htf_period": 20, "ltf_period": 20}),
        ("Structure 2/2 R2", "structure_breakout", {"swing_left": 2, "swing_right": 2}),
    ]
    results = []
    print(f"\n{'='*70}")
    print(f"RESEARCH: {len(candidates)} candidates on XAUUSDm M15")
    print(f"{'='*70}\n")
    for name, strat, params in candidates:
        if args.only and not any(token.lower() in name.lower() for token in args.only):
            continue
        r = run_candidate(
            name,
            strat,
            params,
            bars,
            config,
            permutations=args.permutations,
            seed=args.seed,
        )
        results.append(r)
        s = "PASS" if r.validation_passed else "FAIL"
        print(f"{name}:")
        print(
            f"  IS : {r.is_trades}t PF={r.is_profit_factor:.2f} "
            f"E={r.is_expectancy:.2f} DD={r.is_max_drawdown_pct:.1f}%"
        )
        print(
            f"  OOS: {r.oos_trades}t PF={r.oos_profit_factor:.2f} "
            f"E={r.oos_expectancy:.2f} DD={r.oos_max_drawdown_pct:.1f}%"
        )
        print(f"  WF : pass={r.wf_pass_rate:.0%} meanPF={r.wf_mean_pf:.2f}")
        print(
            f"  MC : profitable={r.mc_pct_profitable:.0%} "
            f"medDD={r.mc_median_dd_pct:.1f}% p95DD={r.mc_p95_dd_pct:.1f}%"
        )
        print(
            f"  SENS: spread_stable={r.spread_stable} param_stable={r.param_stable}"
        )
        print(f"  SIG : oos_mean_r={r.oos_mean_r:+.4f} p={r.p_value}")
        print(
            f"  [{s}] "
            f"{'; '.join(r.rejection_reasons) if r.rejection_reasons else 'ALL OK'}"
        )
        print()

    # Multiplicity control runs over the whole family before anything is called validated: picking
    # the best of N candidates is exactly how a search produces apparent edges from noise.
    multiplicity = apply_multiplicity_pass(results, method=args.method, alpha=args.alpha)
    passed = sum(1 for r in results if r.validation_passed)
    print(f"{'='*70}")
    print(
        f"MULTIPLICITY ({multiplicity.method}, alpha={multiplicity.alpha}): "
        f"{multiplicity.tested} testable, {len(multiplicity.survivors)} survivor(s)"
    )
    for name in multiplicity.survivors:
        print(f"  SURVIVOR {name}: p={multiplicity.raw_p_values[name]:.4f} "
              f"p_adj={multiplicity.adjusted[name]:.4f}")
    if multiplicity.untestable:
        print(f"  not testable (kept out of the family): {', '.join(multiplicity.untestable)}")
    print(f"{'='*70}")
    print(f"RESULTS: {passed}/{len(results)} validated after multiplicity control")
    print(f"{'='*70}")
    out = Path(args.out)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": "XAUUSDm",
        "timeframe": "M15",
        "data_range": [
            bars[0].time.isoformat(),
            bars[-1].time.isoformat(),
        ],
        "total_bars": len(bars),
        "passed": passed,
        "multiplicity": multiplicity.to_dict(),
        "candidates": [r.to_dict() for r in results],
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Report: {out}")
    return 0 if passed > 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
