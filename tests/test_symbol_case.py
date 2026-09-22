"""Symbol case preservation (broker ids are case-sensitive; XAUUSDm != XAUUSDM)."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from mt5_platform.backtest import symbols_match, write_validation
from mt5_platform.backtest.metrics import GateReport, Metrics
from mt5_platform.backtest.validation import live_block_reason
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, OrderRequest, StrategySignal
from mt5_platform.strategy.registry import create_strategy


def _metrics() -> Metrics:
    return Metrics(100, 100, 0, 0, 1, 0.5, 1.5, 1, 1, 1, 1, 1, 0, 1, 1, 1, [])


def test_symbols_match_exact_case() -> None:
    assert symbols_match("XAUUSDm", "XAUUSDm")
    assert not symbols_match("XAUUSDm", "EURUSD")


def test_symbols_match_legacy_uppercase() -> None:
    # Old reports uppercased symbols; runtime still matches without copying the bug.
    assert symbols_match("XAUUSDM", "XAUUSDm")
    assert symbols_match("XAUUSDm", "XAUUSDM")


def test_symbols_match_whitespace() -> None:
    assert symbols_match("  XAUUSDm  ", "XAUUSDm")


def test_market_data_event_preserves_broker_symbol() -> None:
    event = MarketDataEvent(
        timestamp=datetime.now(UTC), source="mt5", symbol="XAUUSDm",
        bid=4320.0, ask=4320.26,
    )
    assert event.symbol == "XAUUSDm", event.symbol


def test_signal_preserves_broker_symbol() -> None:
    signal = StrategySignal(
        symbol="XAUUSDm", direction=OrderSide.BUY,
        entry=4320.0, stop_loss=4315.0, take_profit=4330.0,
        confidence=0.7, strategy_name="breakout",
        timestamp=datetime.now(UTC),
    )
    assert signal.symbol == "XAUUSDm", signal.symbol


def test_order_preserves_broker_symbol() -> None:
    order = OrderRequest(
        symbol="XAUUSDm", side=OrderSide.BUY, volume=0.01,
        entry=4320.0, stop_loss=4315.0, take_profit=4330.0,
    )
    assert order.symbol == "XAUUSDm", order.symbol


def test_strategy_handles_case_insensitively() -> None:
    strat = create_strategy("breakout", lookback=20, symbols={"XAUUSDm"})
    assert strat.handles("XAUUSDm")
    assert strat.handles("XAUUSDM")
    assert not strat.handles("EURUSD")
    free = create_strategy("breakout", lookback=20)
    assert free.handles("XAUUSDm") and free.handles("EURUSD")


def test_live_block_reason_accepts_exact_symbol() -> None:
    gates = GateReport(passed=True, checks=[("t", True, "ok")])
    tmp = Path(tempfile.gettempdir()) / "test_validation_sym.json"
    write_validation(
        tmp, symbol="XAUUSDm", timeframe="M15", strategy="breakout",
        params={}, gates=gates, in_sample=_metrics(), out_of_sample=_metrics(),
        data_range=("2025-01-01", "2025-12-31"),
    )
    stored = json.loads(tmp.read_text())
    assert stored["symbol"] == "XAUUSDm", stored["symbol"]
    stored["passed"] = True
    assert live_block_reason(stored, symbol="XAUUSDm", timeframe="M15") is None
    assert live_block_reason(stored, symbol="XAUUSDm", timeframe="m15") is None


def test_live_block_reason_rejects_other_symbol() -> None:
    report = {"passed": True, "symbol": "XAUUSDm", "timeframe": "M15"}
    assert live_block_reason(report, symbol="EURUSD", timeframe="M15") is not None
