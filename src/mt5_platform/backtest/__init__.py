"""Backtesting: hardened data loading, a no-lookahead engine, metrics, gates, sweeps."""

from mt5_platform.backtest.data import Bar, LoadReport, load_csv, resample, synthetic_bars
from mt5_platform.backtest.engine import BacktestConfig, BacktestResult, Trade, run_backtest
from mt5_platform.backtest.metrics import GateReport, Metrics, compute_metrics, evaluate_gates
from mt5_platform.backtest.research import SweepReport, SweepRow, split_bars, sweep

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
    "load_csv",
    "resample",
    "run_backtest",
    "split_bars",
    "sweep",
    "synthetic_bars",
]
