"""The verdict file: live trading refuses to start without a passing, matching report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from mt5_platform.backtest.metrics import GateReport, Metrics


def write_validation(
    path: str | Path,
    *,
    symbol: str,
    timeframe: str,
    strategy: str,
    params: dict,
    gates: GateReport,
    in_sample: Metrics,
    out_of_sample: Metrics | None,
    data_range: tuple[str, str],
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": gates.passed,
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "strategy": strategy,
        "params": params,
        "data_range": list(data_range),
        "gates": gates.to_dict(),
        "in_sample": in_sample.to_dict(),
        "out_of_sample": out_of_sample.to_dict() if out_of_sample else None,
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_validation(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def live_block_reason(report: dict | None, *, symbol: str, timeframe: str) -> str | None:
    """Why live trading must not start (None = allowed)."""
    if report is None:
        return "no validation report found (run the backtest CLI on real data first)"
    if not report.get("passed"):
        return "the latest validation report FAILED its gates"
    if report.get("symbol") != symbol.upper() or report.get("timeframe") != timeframe.upper():
        return (
            f"validation was for {report.get('symbol')} {report.get('timeframe')}, "
            f"not {symbol.upper()} {timeframe.upper()}"
        )
    return None
