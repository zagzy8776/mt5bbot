"""Research runner: tests strategy candidates on real XAUUSDm M15 data."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mt5_platform.backtest.data import Bar, load_csv
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.backtest.metrics import compute_metrics
from mt5_platform.backtest.research import split_bars
from mt5_platform.common.instruments import InstrumentSpec
from mt5_platform.research.manifest import (
    HOLDOUT_FRACTION,
    HypothesisRecord,
    code_commit,
    dataset_digest,
    family_id_for,
    holdout_state,
    record_holdout_confirmation,
    slug,
    split_dataset,
    verify_manifest_dataset,
    write_manifest,
)
from mt5_platform.research.manifest import (
    build_manifest as build_research_manifest,
)
from mt5_platform.research.manifest import (
    read_manifest as read_research_manifest,
)
from mt5_platform.research.methodology import (
    dependence_diagnostics,
    power_analysis,
    test_description,
    trades_needed_for_edge,
)
from mt5_platform.research.multiplicity import (
    DEFAULT_ALPHA,
    DEFAULT_MIN_TRADES,
    MultiplicityReport,
    apply_multiplicity,
    sensitivity,
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
    # Hypothesis identity (see research/manifest.py) so the searched family is auditable.
    candidate_id: str = ""
    family_id: str = ""
    hypothesis_version: str = "1"
    regime_definition: str = ""
    # Dependence / power diagnostics (see research/methodology.py).
    dependence: dict = field(default_factory=dict)
    power: dict = field(default_factory=dict)

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
    family_id: str = "",
    hypothesis_version: str = "1",
    regime_definition: str = "",
    min_trades: int = DEFAULT_MIN_TRADES,
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
        candidate_id=slug(name),
        family_id=family_id,
        hypothesis_version=hypothesis_version,
        regime_definition=regime_definition,
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
    # Raw trade count is not the sample size: overlap, clustering and serial dependence decide how
    # much independent information this window actually carries.
    report.dependence = dependence_diagnostics(
        [(trade.opened_at, trade.closed_at, trade.r_multiple) for trade in oos_result.trades],
        min_trades=min_trades,
    ).to_dict()
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


def apply_dependence_gate(
    results: list[CandidateReport], *, min_trades: int = DEFAULT_MIN_TRADES
) -> list[str]:
    """Refuse to treat a p-value as evidence when the *effective* sample cannot support the test.

    Overlapping trades and serial dependence mean the raw count overstates the information in the
    window. A candidate whose effective sample is below the test minimum keeps its diagnostics but
    cannot be validated: its p-value would be optimistic, not conservative.
    """
    flagged: list[str] = []
    for candidate in results:
        n_eff = float(candidate.dependence.get("n_effective") or 0.0)
        if candidate.validation_passed and n_eff < min_trades:
            candidate.validation_passed = False
            candidate.rejection_reasons = [
                *candidate.rejection_reasons,
                f"insufficient_effective_sample:n_eff={n_eff:.1f}<{min_trades}",
            ]
            flagged.append(candidate.name)
    return flagged


def apply_multiplicity_pass(
    results: list[CandidateReport],
    *,
    method: str = "benjamini-hochberg",
    alpha: float = DEFAULT_ALPHA,
) -> MultiplicityReport:
    """Correct the family of candidates and fold the verdict into ``validation_passed``.

    A candidate that passed every other gate but is not a multiplicity survivor is marked as failed
    with an explicit reason, so no downstream reader can accidentally treat it as validated. Each
    candidate also gets a power block: the smallest edge this sample could have detected at the
    family's smallest-rank threshold. Failing to reject the null is not evidence of absence.
    """
    report = apply_multiplicity(
        {candidate.name: candidate.p_value for candidate in results},
        method=method,
        alpha=alpha,
    )
    survivors = set(report.survivors)
    alpha_rank1 = report.alpha / max(report.tested, 1)
    for candidate in results:
        candidate.multiplicity_method = report.method
        candidate.p_value_adjusted = report.adjusted.get(candidate.name)
        candidate.multiplicity_survivor = candidate.name in survivors
        n_eff = float(candidate.dependence.get("n_effective") or candidate.oos_trades or 0.0)
        candidate.power = power_analysis(n_effective=n_eff, alpha_rank1=alpha_rank1).to_dict()
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
        choices=("benjamini-hochberg", "benjamini-yekutieli", "bonferroni"),
        help="primary correction; the others are always reported as labelled sensitivities",
    )
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--family-id", default="", help="identifier of the search family")
    parser.add_argument("--hypothesis-version", default="1")
    parser.add_argument(
        "--holdout-fraction",
        type=float,
        default=HOLDOUT_FRACTION,
        help="most recent fraction of bars sealed as the final holdout (0 disables sealing)",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="re-run the experiment described by a manifest (verifies the dataset hash first)",
    )
    parser.add_argument("--manifest-out", default=r"C:\mt5bbot\data\research_manifest.json")
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="run the same candidates ONCE on the sealed holdout and record the confirmation",
    )
    parser.add_argument(
        "--force-holdout",
        action="store_true",
        help="overwrite an existing holdout confirmation (only when the previous record is void)",
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

    # Reproducibility: a manifest defines the experiment, and the data must still match its hash.
    loaded_manifest = None
    if args.manifest:
        loaded_manifest = read_research_manifest(args.manifest)
        args.csv = loaded_manifest.dataset_path or args.csv
        args.symbol = loaded_manifest.symbol or args.symbol
        args.timeframe = loaded_manifest.timeframe or args.timeframe
        args.family_id = loaded_manifest.family_id or args.family_id
        args.hypothesis_version = loaded_manifest.hypothesis_version or args.hypothesis_version
        args.alpha = loaded_manifest.correction.get("alpha") or args.alpha
        args.method = loaded_manifest.correction.get("primary") or args.method
        args.permutations = loaded_manifest.statistics.get("permutations") or args.permutations
        args.seed = loaded_manifest.statistics.get("seed") or args.seed
        args.holdout_fraction = loaded_manifest.holdout.get(
            "holdout_fraction", args.holdout_fraction
        )
        print(f"loaded manifest: {args.manifest}")

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found. Run fetch-mt5 first.")
        return 1
    loaded = load_csv(csv_path)
    all_bars = loaded.bars
    print(f"Loaded {len(all_bars)} bars: {all_bars[0].time} to {all_bars[-1].time}")

    if loaded_manifest is not None:
        matches, detail = verify_manifest_dataset(loaded_manifest, all_bars)
        print(f"manifest dataset check: {detail}")
        if not matches:
            return 1

    # DISCOVERY + VALIDATION are for selection; the most recent slice is sealed, never selected on.
    bars, split, holdout_bars = split_dataset(all_bars, holdout_fraction=args.holdout_fraction)
    print(
        f"Research window: {len(bars)} bars "
        f"(discovery {split.discovery_bars} / validation {split.validation_bars}); "
        f"sealed holdout: {split.holdout_bars} bars "
        f"{split.holdout_start[:10]}..{split.holdout_end[:10]}"
    )

    commit = code_commit()
    family_id = args.family_id or family_id_for(
        symbol=args.symbol,
        timeframe=args.timeframe,
        hypothesis_version=args.hypothesis_version,
        code_commit=commit,
    )
    costs = {"spread_price": 0.26, "slippage_price": 0.05, "commission_per_lot": 0.0}
    backtest_settings = {
        "starting_balance": 100_000.0,
        "risk_pct": 1.0,
        "max_volume": 0.10,
        "max_exposure_pct": 300.0,
    }
    spec = InstrumentSpec(
        "XAUUSDm", 100.0, 0.001, 0.1, volume_min=0.01, volume_step=0.01, digits=3
    )
    config = BacktestConfig(
        symbol=args.symbol,
        spec=spec,
        starting_balance=backtest_settings["starting_balance"],
        risk_pct=backtest_settings["risk_pct"],
        max_volume=backtest_settings["max_volume"],
        default_spread_price=costs["spread_price"],
        slippage_price=costs["slippage_price"],
        max_exposure_pct=backtest_settings["max_exposure_pct"],
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
        # F. Contract 8.2 (docs/research-contract-8.2.md): the gold scalp-shaped family, fixed at 12
        # candidates before the run. Stops are wide (0.30% / 0.50%) because the cost audit shows a
        # sub-0.15% stop surrenders ~half of a 0.10R edge to gold's ~240-point spread.
        ("Scalp MB 10", "scalp_micro_breakout",
         {"lookback": 10, "atr_buffer": 0.25, "stop_loss_pct": 0.5, "take_profit_pct": 1.0}),
        ("Scalp MB 20", "scalp_micro_breakout",
         {"lookback": 20, "atr_buffer": 0.25, "stop_loss_pct": 0.5, "take_profit_pct": 1.0}),
        ("Scalp MB 20 b50", "scalp_micro_breakout",
         {"lookback": 20, "atr_buffer": 0.5, "stop_loss_pct": 0.5, "take_profit_pct": 1.0}),
        ("Scalp MB 40 tight", "scalp_micro_breakout",
         {"lookback": 40, "atr_buffer": 0.5, "stop_loss_pct": 0.3, "take_profit_pct": 1.0}),
        ("Scalp REV 20 z1.0", "scalp_vwap_reversion",
         {"window": 20, "deviations": 1.0, "stop_loss_pct": 0.5, "take_profit_pct": 1.0}),
        ("Scalp REV 20 z1.5", "scalp_vwap_reversion",
         {"window": 20, "deviations": 1.5, "stop_loss_pct": 0.5, "take_profit_pct": 1.0}),
        ("Scalp REV 40 z1.5", "scalp_vwap_reversion",
         {"window": 40, "deviations": 1.5, "stop_loss_pct": 0.3, "take_profit_pct": 1.0}),
        ("Scalp REV 40 z2.0", "scalp_vwap_reversion",
         {"window": 40, "deviations": 2.0, "stop_loss_pct": 0.3, "take_profit_pct": 1.5}),
        ("Scalp MOM 07", "scalp_session_momentum",
         {"session_start_hour": 7, "session_end_hour": 9, "stop_loss_pct": 0.5,
          "take_profit_pct": 1.0}),
        ("Scalp MOM 13", "scalp_session_momentum",
         {"session_start_hour": 13, "session_end_hour": 15, "stop_loss_pct": 0.5,
          "take_profit_pct": 1.0}),
        ("Scalp MOM 07 13", "scalp_session_momentum",
         {"session_start_hour": 7, "session_end_hour": 15, "stop_loss_pct": 0.5,
          "take_profit_pct": 1.5}),
        ("Scalp MOM 15", "scalp_session_momentum",
         {"session_start_hour": 15, "session_end_hour": 17, "stop_loss_pct": 0.3,
          "take_profit_pct": 1.0}),
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
            family_id=family_id,
            hypothesis_version=args.hypothesis_version,
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
        print(
            f"  SIG : oos_mean_r={r.oos_mean_r:+.4f} p={r.p_value} "
            f"n_eff={r.dependence.get('n_effective', 0):.1f}/{r.dependence.get('n_raw', 0)}"
        )
        if r.dependence.get("flags"):
            print(f"  DEP : {'; '.join(r.dependence['flags'])}")
        print(
            f"  [{s}] "
            f"{'; '.join(r.rejection_reasons) if r.rejection_reasons else 'ALL OK'}"
        )
        print()

    # A p-value the effective sample cannot support is not evidence: gate on dependence first, then
    # correct the family. Both steps only ever remove candidates.
    effective_flagged = apply_dependence_gate(results)
    if effective_flagged:
        print(f"EFFECTIVE SAMPLE: {', '.join(effective_flagged)} below the test minimum")
    multiplicity = apply_multiplicity_pass(results, method=args.method, alpha=args.alpha)
    sens = sensitivity({r.name: r.p_value for r in results}, alpha=args.alpha)
    passed = sum(1 for r in results if r.validation_passed)
    print(f"{'='*70}")
    print(
        f"MULTIPLICITY primary={multiplicity.method}, alpha={multiplicity.alpha}: "
        f"{multiplicity.tested} testable, {len(multiplicity.survivors)} survivor(s)"
    )
    for name in multiplicity.survivors:
        print(
            f"  SURVIVOR {name}: p={multiplicity.raw_p_values[name]:.4f} "
            f"p_adj={multiplicity.adjusted[name]:.4f}"
        )
    if multiplicity.untestable:
        print(f"  not testable (kept out of the family): {', '.join(multiplicity.untestable)}")
    print("  sensitivity (reported, never used to pick a friendlier answer):")
    for method, method_report in sens.items():
        print(
            f"    {method:22s} alpha={method_report.alpha:.3f} "
            f"survivors={len(method_report.survivors)}"
        )
    print(f"{'='*70}")
    print(f"RESULTS: {passed}/{len(results)} validated after multiplicity control")
    print(f"{'='*70}")

    alpha_rank1 = multiplicity.alpha / max(multiplicity.tested, 1)
    n_eff_values = sorted(float(r.dependence.get("n_effective") or 0.0) for r in results)
    median_n_eff = n_eff_values[len(n_eff_values) // 2] if n_eff_values else 0.0
    conclusion = {
        "verdict": "survivors_found" if passed else "no_candidate_survived",
        "validated": [r.name for r in results if r.validation_passed],
        "raw_gate_survivors": [
            r.name
            for r in results
            if any(reason.startswith("fails_multiplicity") for reason in r.rejection_reasons)
        ],
        "effective_sample_rejections": effective_flagged,
        "primary_correction": {"method": multiplicity.method, "alpha": multiplicity.alpha},
        "survivors_by_method": {name: rep.survivors for name, rep in sens.items()},
        "alpha_rank1": alpha_rank1,
        "median_n_effective": median_n_eff,
        "smallest_detectable_edge_r": power_analysis(
            n_effective=median_n_eff or 1.0, alpha_rank1=alpha_rank1
        ).smallest_detectable_edge_r,
        "trades_needed_for_0.05r_edge": trades_needed_for_edge(0.05, alpha_rank1=alpha_rank1),
        "interpretation": (
            "no candidate survived the family-corrected threshold. This is a failure to reject the "
            "null at the multiplicity-adjusted threshold, NOT evidence that no edge exists: the "
            "power block reports the smallest edge this sample could have detected."
            if not passed
            else "at least one candidate survived; it still has to earn forward evidence before it "
            "can be considered for configuration"
        ),
    }
    family = {
        "family_id": family_id,
        "hypothesis_version": args.hypothesis_version,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "candidate_count": len(results),
        "regime_definition": "",
        "candidates": [
            HypothesisRecord(
                candidate_id=r.candidate_id,
                family_id=family_id,
                label=r.name,
                symbol=args.symbol,
                timeframe=args.timeframe,
                regime_definition=r.regime_definition,
                parameter_set=r.params,
                hypothesis_version=args.hypothesis_version,
            ).to_dict()
            for r in results
        ],
    }
    holdout_block = {
        **split.to_dict(),
        "confirmations_on_record": len(
            [
                row
                for row in holdout_state()["confirmations"]
                if row.get("family_id") == family_id
            ]
        ),
    }
    if args.confirm_holdout:
        if not holdout_bars:
            print("holdout confirmation skipped: this dataset has no sealed holdout")
        else:
            print("running the sealed holdout ONCE (this is evidence about the process)...")
            selected = [
                (name, strat, params)
                for name, strat, params in candidates
                if not args.only or any(token.lower() in name.lower() for token in args.only)
            ]
            holdout_results = [
                run_candidate(
                    name,
                    strat,
                    params,
                    holdout_bars,
                    config,
                    permutations=args.permutations,
                    seed=args.seed,
                    family_id=family_id,
                    hypothesis_version=args.hypothesis_version,
                )
                for name, strat, params in selected
            ]
            survivors = [r.name for r in holdout_results if r.validation_passed]
            record = record_holdout_confirmation(
                family_id=family_id,
                dataset_sha256=dataset_digest(all_bars),
                holdout_sha256=split.holdout_sha256,
                survivors=survivors,
                results={
                    r.name: {
                        "oos_mean_r": r.oos_mean_r,
                        "oos_trades": r.oos_trades,
                        "validation_passed": r.validation_passed,
                    }
                    for r in holdout_results
                },
                force=args.force_holdout,
            )
            holdout_block["confirmation"] = record
            print(f"holdout confirmation recorded: {len(survivors)} survivor(s)")

    statistics = test_description(
        permutations=args.permutations, seed=args.seed, min_trades=DEFAULT_MIN_TRADES
    )
    statistics["effective_sample_gate"] = DEFAULT_MIN_TRADES
    manifest = build_research_manifest(
        dataset_path=csv_path,
        all_bars=all_bars,
        research_bars=bars,
        symbol=args.symbol,
        timeframe=args.timeframe,
        costs=costs,
        backtest=backtest_settings,
        family=family,
        statistics=statistics,
        correction={
            "primary": multiplicity.method,
            "alpha": args.alpha,
            "sensitivity": {
                name: {"alpha": rep.alpha, "survivors": rep.survivors}
                for name, rep in sens.items()
            },
        },
        holdout=holdout_block,
    )
    out = Path(args.out)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "data_range": [bars[0].time.isoformat(), bars[-1].time.isoformat()],
        "total_bars": len(bars),
        "passed": passed,
        "methodology": statistics,
        "multiplicity": multiplicity.to_dict(),
        "sensitivity": {name: rep.to_dict() for name, rep in sens.items()},
        "family": family,
        "holdout": holdout_block,
        "manifest": manifest.to_dict(),
        "conclusion": conclusion,
        "candidates": [r.to_dict() for r in results],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    payload["report_sha256"] = digest
    manifest.report_sha256 = digest
    payload["manifest"] = manifest.to_dict()
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    manifest_path = write_manifest(manifest, args.manifest_out)
    print(f"Report: {out}")
    print(f"Manifest: {manifest_path}")
    print(f"Conclusion: {conclusion['interpretation']}")
    return 0 if passed > 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
