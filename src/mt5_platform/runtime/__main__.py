"""CLI: python -m mt5_platform.runtime {check,run}   (run on the machine that runs MT5)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.validation import live_block_reason, read_validation
from mt5_platform.common.instruments import min_equity_for_min_lot
from mt5_platform.config import get_settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime.feed import MT5CandleFeed
from mt5_platform.runtime.loop import TradingLoop
from mt5_platform.signals import SignalEngine
from mt5_platform.storage import create_store_from_settings
from mt5_platform.strategy.registry import create_strategy


async def _check(args: argparse.Namespace) -> int:
    settings = get_settings()
    adapter = MT5ExecutionAdapter(settings)
    await adapter.connect()  # refuses real accounts unless live mode is acknowledged
    try:
        acct = await adapter.get_account()
        info = await adapter.call("account_info")
        spec = await adapter.get_instrument(args.symbol)
        mode = "DEMO" if info.trade_mode == adapter.mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL"
        print(
            f"account {info.login} ({mode}) balance {acct.balance:.2f} equity {acct.equity:.2f} "
            f"{getattr(info, 'currency', '')}"
        )
        if spec is None:
            print(f"symbol {args.symbol} not found (some accounts add a suffix, e.g. XAUUSDm)")
            return 2
        print(
            f"{spec.symbol}: contract {spec.contract_size:g}, min lot {spec.volume_min:g}, "
            f"step {spec.volume_step:g}, tick {spec.tick_size:g} = {spec.tick_value:g}"
        )
        per_dollar = spec.value_per_price_unit * spec.volume_min
        print(f"at the minimum lot, every 1.00 price move is {per_dollar:.4f} in account currency")
        print(f"\nminimum equity to risk <= {args.risk_pct:g}% at the minimum lot:")
        for stop in (1.0, 2.0, 3.0, 5.0, 10.0):
            need = min_equity_for_min_lot(stop_distance=stop, risk_pct=args.risk_pct, spec=spec)
            verdict = "OK" if acct.equity >= need else "TOO SMALL"
            print(f"  stop {stop:>5.2f}: needs {need:>10,.2f}  -> {verdict}")
    finally:
        await adapter.disconnect()
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.execution_backend != "mt5":
        print("Set EXECUTION_BACKEND=mt5 in .env (the mock backend places no real orders).")
        return 2
    report = read_validation(settings.validation_report_path)
    if settings.is_live:
        reason = live_block_reason(report, symbol=args.symbol, timeframe=args.timeframe)
        if reason:
            print(f"LIVE TRADING BLOCKED: {reason}")
            return 2
    from mt5_platform.backtest.validation import symbols_match
    report_symbol = str(report.get("symbol") or "") if report else ""
    if report and report.get("passed") and symbols_match(report_symbol, args.symbol):
        strategies = [create_strategy(report["strategy"], **report["params"])]
        print(f"using validated strategy {report['strategy']} {report['params']}")
    else:
        strategies = [create_strategy(n) for n in settings.active_strategies]
        print("WARNING: no passing validation report; running default strategies on DEMO only")

    adapter = MT5ExecutionAdapter(settings)
    risk = RiskEngine(settings=settings)
    store = create_store_from_settings(settings)
    engine = getattr(store, "_engine", None)
    if engine is not None:
        from mt5_platform.storage.db import init_db
        await init_db(engine)
    from mt5_platform.signals import AuditStoreSink, SignalStoreSink
    signal_engine = SignalEngine(
        strategies,
        min_confidence=settings.signal_min_confidence,
        require_stop_loss=True,
        cooldown_s=settings.signal_cooldown_s,
        sink=SignalStoreSink(store),
        audit_sink=AuditStoreSink(store),
    )
    intelligence = None
    if getattr(settings, "intelligence_enabled", False):
        from mt5_platform.runtime.intelligence import IntelligenceLayer
        intelligence = IntelligenceLayer(symbol=args.symbol, timeframe=args.timeframe)
        print(f"intelligence layer ENABLED for {args.symbol}")
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=MT5CandleFeed(adapter, args.timeframe),
        signal_engine=signal_engine,
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, store=store, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=[args.symbol],
        risk_pct=args.risk_pct,
        poll_s=args.poll,
        intelligence=intelligence,
    )
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows event loops
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    print(
        f"running {args.symbol} {args.timeframe}; Ctrl+C to stop "
        "(open positions keep their broker-side SL/TP)"
    )
    try:
        await loop.run(stop)
    except KeyboardInterrupt:
        pass
    finally:
        print("stats:", loop.stats.to_dict())
        await adapter.disconnect()
        if engine is not None:
            await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mt5_platform.runtime")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", default="XAUUSD")
        p.add_argument("--timeframe", default="M15")
        p.add_argument("--risk-pct", type=float, default=1.0)
        if name == "run":
            p.add_argument("--poll", type=float, default=5.0)
    args = parser.parse_args(argv)
    return asyncio.run(_check(args) if args.cmd == "check" else _run(args))


if __name__ == "__main__":
    sys.exit(main())
