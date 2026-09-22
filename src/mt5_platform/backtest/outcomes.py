"""Backtest trades -> HistoricalOutcome: one outcome schema for research and live.

This is a normalizer, not an engine rewrite. Live trades and backtest trades then share the same
``HistoricalOutcome`` model, so later research can compare backtest outcomes against forward demo
outcomes, strategy versions, regimes, MAE/MFE and exit causes on identical fields.

Honest gaps: the bar engine does not track intra-trade excursions or account-relative returns, so
those stay explicitly unavailable (``excursions_available = False``) instead of being invented.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.common.enums import ExitCause, OutcomeSource, OutcomeStatus
from mt5_platform.historical.models import HistoricalOutcome, SetupFeatures

# The engine's exit vocabulary ("sl" | "tp" | "end") and its cause taxonomy.
BACKTEST_EXIT_CAUSES: dict[str, ExitCause] = {
    "sl": ExitCause.STOP_LOSS,
    "tp": ExitCause.TAKE_PROFIT,
    "end": ExitCause.TIME_STOP,
    "time": ExitCause.TIME_STOP,
    "manual": ExitCause.MANUAL,
}


def exit_cause_from_backtest(reason: str) -> ExitCause:
    """Map an engine exit reason onto the shared taxonomy (unknown stays unknown)."""
    return BACKTEST_EXIT_CAUSES.get(str(reason or "").strip().lower(), ExitCause.UNKNOWN)


def outcome_from_backtest_trade(
    trade: Any,
    *,
    symbol: str,
    timeframe: str = "",
    strategy_version: str = "",
    return_pct: float | None = None,
    regime: Any | None = None,
) -> HistoricalOutcome:
    """Normalize one engine trade into the canonical outcome record."""
    exit_cause = exit_cause_from_backtest(getattr(trade, "exit_reason", ""))
    try:
        duration = (trade.closed_at - trade.opened_at).total_seconds()
    except (AttributeError, TypeError):  # pragma: no cover - malformed trade
        duration = 0.0
    features = SetupFeatures(
        instrument=symbol,
        timestamp=trade.opened_at,
        timeframe=timeframe or "unspecified",
        regime=regime,
        strategy=str(getattr(trade, "strategy", "")),
        strategy_version=strategy_version,
        data_quality="ok",
    )
    record = HistoricalOutcome(
        status=OutcomeStatus.CLOSED,
        instrument=symbol,
        timeframe=timeframe,
        strategy=str(getattr(trade, "strategy", "")),
        strategy_version=strategy_version,
        direction=trade.side,
        source=OutcomeSource.BACKTEST,
        timestamp=trade.opened_at,
        entry=float(trade.entry),
        exit_price=float(trade.exit),
        exit_time=trade.closed_at,
        exit_cause=exit_cause,
        exit_cause_source="backtest",
        stop_loss=float(trade.stop_loss) if trade.stop_loss else None,
        entry_volume=float(trade.volume),
        remaining_volume=0.0,
        realized_pnl=float(trade.pnl),
        return_pct=float(return_pct) if return_pct is not None else 0.0,
        realized_pnl_pct=float(return_pct) if return_pct is not None else None,
        r_multiple=float(trade.r_multiple) if trade.r_multiple is not None else None,
        duration_s=max(0.0, duration),
        features=features,
        evidence={
            "population": "backtest",
            "excursions_available": False,  # the bar engine does not track MAE/MFE
            "return_pct_source": "reported" if return_pct is not None else "unavailable",
            "realized_pnl_source": "backtest_engine",
            "exit_reason_raw": str(getattr(trade, "exit_reason", "")),
        },
    )
    record.cause_class = record.exit_reason
    return record


def outcomes_from_backtest_result(
    result: Any,
    *,
    symbol: str | None = None,
    timeframe: str = "",
    strategy_version: str = "",
) -> list[HistoricalOutcome]:
    """Normalize a whole backtest result. The config's symbol keeps its exact broker case."""
    resolved = symbol or str(result.config.symbol)
    return [
        outcome_from_backtest_trade(
            trade,
            symbol=resolved,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        for trade in result.trades
    ]


__all__ = [
    "BACKTEST_EXIT_CAUSES",
    "exit_cause_from_backtest",
    "outcome_from_backtest_trade",
    "outcomes_from_backtest_result",
]
