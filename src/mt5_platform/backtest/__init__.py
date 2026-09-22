"""Backtesting: hardened data loading, a no-lookahead engine, metrics, gates, sweeps."""

from mt5_platform.backtest.data import Bar, LoadReport, load_csv, resample, synthetic_bars
from mt5_platform.backtest.engine import BacktestConfig, BacktestResult, Trade, run_backtest
from mt5_platform.backtest.metrics import GateReport, Metrics, compute_metrics, evaluate_gates
from mt5_platform.backtest.research import SweepReport, SweepRow, split_bars, sweep
from mt5_platform.backtest.validation import (
    live_block_reason,
    read_validation,
    symbols_match,
    write_validation,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Bar",
    "GateReport",
    "LoadReport",
    "Metrics",
    "SweepReport",
    "SweepRow",
    "Trade",
    "compute_metrics",
    "evaluate_gates",
    "live_block_reason",
    "load_csv",
    "read_validation",
    "resample",
    "run_backtest",
    "split_bars",
    "sweep",
    "symbols_match",
    "synthetic_bars",
    "write_validation",
]
