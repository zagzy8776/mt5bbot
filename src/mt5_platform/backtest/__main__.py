"""CLI: python -m mt5_platform.backtest {synth,fetch-mt5,fetch-dukascopy,run,sweep} ..."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mt5_platform.backtest.data import (
    fetch_mt5_bars,
    load_csv,
    synthetic_bars,
    timeframe_minutes,
    write_csv,
)
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.backtest.metrics import Metrics, compute_metrics, evaluate_gates
from mt5_platform.backtest.research import split_bars, sweep
from mt5_platform.backtest.validation import write_validation
from mt5_platform.common.instruments import InstrumentSpec, min_equity_for_min_lot
from mt5_platform.strategy.registry import create_strategy


def _coerce(text: str):
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(text.lower(), text)


def _kv(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        key, _, val = item.partition("=")
        if not key or not val:
            raise SystemExit(f"expected name=value, got {item!r}")
        out[key.strip()] = _coerce(val.strip())
    return out


def _grid(items: list[str]) -> dict[str, list]:
    out = {}
    for item in items or []:
        key, _, val = item.partition("=")
        out[key.strip()] = [_coerce(v.strip()) for v in val.split(",") if v.strip()]
    if not out:
        raise SystemExit("sweep needs at least one --grid name=v1,v2,...")
    return out


def _config(a: argparse.Namespace) -> BacktestConfig:
    spec = None
    if a.contract_size:
        spec = InstrumentSpec(
            a.symbol.strip(),
            a.contract_size,
            a.tick_size,
            a.tick_value,
            volume_min=a.vol_min,
            volume_step=a.vol_step,
        )
    return BacktestConfig(
        symbol=a.symbol,
        spec=spec,
        starting_balance=a.balance,
        risk_pct=a.risk_pct,
        max_volume=a.max_volume,
        default_spread_price=a.spread,
        slippage_price=a.slippage,
        commission_per_lot=a.commission,
        max_exposure_pct=a.max_exposure,
    )


def _print_metrics(label: str, m: Metrics, target: float) -> None:
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    print(f"\n== {label} ==")
    print(f"  trades {m.n_trades} | win rate {m.win_rate:.0%} | profit factor {pf}")
    print(
        f"  net {m.net_profit:+.2f} ({m.return_pct:+.1f}%) | max drawdown {m.max_drawdown_pct:.1f}%"
        f" | longest losing streak {m.longest_losing_streak}"
    )
    print(f"  expectancy {m.expectancy_money:+.4f}/trade ({m.expectancy_r:+.2f}R)")
    print(
        f"  weeks {m.weeks} | avg week {m.avg_weekly_pnl:+.2f} | profitable weeks "
        f"{m.pct_weeks_profitable:.0%} | weeks >= {target:g}: {m.weeks_at_or_above(target):.0%}"
    )


def _cmd_run(a: argparse.Namespace) -> int:
    report = load_csv(a.csv)
    print(report.summary())
    if len(report.bars) < 500:
        print("Too little data for a meaningful test (need at least a few months).")
        return 2
    params = _kv(a.param)
    cfg = _config(a)
    train, test = split_bars(report.bars, a.oos)
    res_is = run_backtest(train, [create_strategy(a.strategy, **params)], cfg)
    res_oos = run_backtest(test, [create_strategy(a.strategy, **params)], cfg)
    m_is, m_oos = compute_metrics(res_is), compute_metrics(res_oos)
    print(
        f"\n{a.strategy} {params or '(defaults)'} on {a.symbol} {a.timeframe}, "
        f"start balance {cfg.starting_balance:g}, risk {cfg.risk_pct:g}%/trade"
    )
    _print_metrics(f"IN-SAMPLE ({len(train)} bars)", m_is, a.weekly_target)
    _print_metrics(f"OUT-OF-SAMPLE ({len(test)} bars)", m_oos, a.weekly_target)
    for label, res in (("in-sample", res_is), ("out-of-sample", res_oos)):
        if res.skips:
            print(
                f"  skipped signals ({label}): "
                + ", ".join(f"{k}={v}" for k, v in res.skips.items())
            )
        if res.halted:
            print(f"  HALTED ({label}): {res.halt_reason}")
        if res.ruined:
            print(f"  ACCOUNT WIPED OUT ({label})")
    if m_is.n_trades == 0 and res_is.skips.get("size_too_small"):
        spec = cfg.resolved_spec()
        need = min_equity_for_min_lot(stop_distance=5.0, risk_pct=cfg.risk_pct, spec=spec)
        print(
            f"\nThis account is too small for the strategy's stops: "
            f"no trade fits {cfg.risk_pct:g}% risk. "
            f"For reference, a $5.00 stop at the minimum lot needs about {need:,.0f} equity."
        )
    gates = evaluate_gates(m_is, m_oos)
    print("\nGates (necessary, not sufficient):")
    print("\n".join(gates.lines()))
    print(
        "\nVERDICT:",
        "passed all gates (still forward-test on demo)"
        if gates.passed
        else "NOT READY for live money",
    )
    write_validation(
        a.validation_out,
        symbol=a.symbol,
        timeframe=a.timeframe,
        strategy=a.strategy,
        params=params,
        gates=gates,
        in_sample=m_is,
        out_of_sample=m_oos,
        data_range=(f"{report.bars[0].time:%Y-%m-%d}", f"{report.bars[-1].time:%Y-%m-%d}"),
    )
    print(f"validation report written to {a.validation_out}")
    return 0 if gates.passed else 1


def _cmd_sweep(a: argparse.Namespace) -> int:
    report = load_csv(a.csv)
    print(report.summary())
    out = sweep(a.strategy, _grid(a.grid), report.bars, _config(a), oos_fraction=a.oos, top_k=a.top)
    print(f"\ntested {out.combos_tested} parameter sets ({out.combos_invalid} invalid)")
    if out.multiple_testing_warning:
        print("WARNING:", out.multiple_testing_warning)
    print(
        f"\n{'params':<44}{'IS PF':>7}{'IS n':>6}{'OOS PF':>8}"
        f"{'OOS n':>7}{'OOS net':>9}{'degrade':>9}"
    )
    for r in out.rows:
        o = r.out_of_sample
        deg = f"{r.degradation:.2f}" if r.degradation is not None else "n/a"
        print(
            f"{str(r.params):<44}{r.in_sample.profit_factor:>7.2f}{r.in_sample.n_trades:>6}"
            f"{(o.profit_factor if o else 0):>8.2f}{(o.n_trades if o else 0):>7}"
            f"{(o.net_profit if o else 0):>+9.2f}{deg:>9}"
        )
    print(
        "\nPick by out-of-sample, then confirm with `run --param ...` "
        "on data you have not tuned on."
    )
    return 0


def _cmd_fetch_mt5(a: argparse.Namespace) -> int:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 package not available (Windows-only). Use fetch-dukascopy elsewhere.")
        return 2
    end = datetime.now(UTC)
    bars = fetch_mt5_bars(mt5, a.symbol, a.timeframe, end - timedelta(days=a.days), end)
    mt5.shutdown()
    write_csv(bars, a.out)
    print(f"wrote {len(bars)} bars to {a.out}")
    return 0


def _cmd_fetch_duka(a: argparse.Namespace) -> int:
    from mt5_platform.backtest.dukascopy import download_range

    end = datetime.now(UTC).date() - timedelta(days=1)
    bars, stats = download_range(
        a.symbol, end - timedelta(days=a.days), end, timeframe=a.timeframe, cache_dir=a.cache_dir
    )
    write_csv(bars, a.out)
    print(f"wrote {len(bars)} bars to {a.out}; stats: {stats}")
    return 0


def _cmd_synth(a: argparse.Namespace) -> int:
    bars = synthetic_bars(a.n, seed=a.seed, minutes=timeframe_minutes(a.timeframe))
    write_csv(bars, a.out)
    print(f"wrote {len(bars)} synthetic (random-walk, NO edge) bars to {a.out}")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--strategy", default="sma_crossover")
    p.add_argument("--balance", type=float, default=15.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--max-volume", type=float, default=0.10)
    p.add_argument("--max-exposure", type=float, default=300.0)
    p.add_argument(
        "--spread", type=float, default=0.30, help="price units, used when bars have none"
    )
    p.add_argument("--slippage", type=float, default=0.05, help="price units, adverse")
    p.add_argument("--commission", type=float, default=0.0, help="per lot, round turn")
    p.add_argument("--oos", type=float, default=0.3, help="out-of-sample fraction")
    p.add_argument("--contract-size", type=float, default=0.0, help="override instrument spec")
    p.add_argument("--tick-size", type=float, default=0.01)
    p.add_argument("--tick-value", type=float, default=1.0)
    p.add_argument("--vol-min", type=float, default=0.01)
    p.add_argument("--vol-step", type=float, default=0.01)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mt5_platform.backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="backtest one strategy with an out-of-sample split + gates")
    p.add_argument("--csv", required=True)
    p.add_argument("--param", action="append", help="strategy parameter, name=value")
    p.add_argument("--weekly-target", type=float, default=100.0)
    p.add_argument("--validation-out", default="validation_report.json")
    _common(p)
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("sweep", help="parameter sweep ranked in-sample, verified out-of-sample")
    p.add_argument("--csv", required=True)
    p.add_argument("--grid", action="append", help="name=v1,v2,v3")
    p.add_argument("--top", type=int, default=5)
    _common(p)
    p.set_defaults(fn=_cmd_sweep)

    p = sub.add_parser("fetch-mt5", help="download broker history via MT5 (Windows)")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--out", default="data/xauusd_m15.csv")
    p.set_defaults(fn=_cmd_fetch_mt5)

    p = sub.add_parser("fetch-dukascopy", help="download free M1 history, resample, write CSV")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--out", default="data/xauusd_m15.csv")
    p.add_argument("--cache-dir", default="data/cache/dukascopy")
    p.set_defaults(fn=_cmd_fetch_duka)

    p = sub.add_parser("synth", help="write random-walk bars for smoke tests (no edge)")
    p.add_argument("--out", default="data/synthetic_m15.csv")
    p.add_argument("--n", type=int, default=8000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--timeframe", default="M15")
    p.set_defaults(fn=_cmd_synth)

    args = parser.parse_args(argv)
    Path("data").mkdir(exist_ok=True)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
