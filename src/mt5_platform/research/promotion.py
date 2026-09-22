"""Promotion bridge: research evidence -> an explicitly approved runtime configuration.

Nothing here is automatic. A candidate becomes eligible only on the evidence the research runner
already produced — its own ``validation_passed`` verdict and an empty ``rejection_reasons`` list.
This module adds no gates and lowers none; it copies the report's metrics and gate outcomes
verbatim so an operator approves on the same numbers the research produced.

Approval is a deliberate, human act: it writes a configuration version record and an
active-strategy file that the runtime only reads when it is explicitly pointed at it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mt5_platform.common.ids import new_correlation_id

# Reports written before the runner recorded the factory key carried only a human label. This table
# resolves those labels; a report that carries ``strategy`` always wins over it.
_LABEL_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("donchian", "breakout"),
    ("sma", "sma_crossover"),
    ("meanrev", "mean_reversion"),
    ("momentum", "momentum"),
    ("atr", "atr_breakout"),
    ("ema", "ema_adx_trend"),
    ("bollinger", "bollinger_reversion"),
    ("rsi", "rsi_ema_pullback"),
    ("session", "session_breakout"),
    ("mtf", "mtf_trend"),
    ("structure", "structure_breakout"),
)


def strategy_for_label(label: str) -> str | None:
    """Resolve a research label (e.g. "Donchian 30") to a strategy factory name."""
    lowered = (label or "").strip().lower()
    for prefix, strategy in _LABEL_STRATEGIES:
        if lowered.startswith(prefix):
            return strategy
    return None


@dataclass
class PromotionProposal:
    """One candidate offered for promotion, with everything a reviewer needs to decide."""

    proposal_id: str
    label: str
    strategy: str
    params: dict[str, Any]
    symbol: str
    timeframe: str
    data_range: tuple[str, str]
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "label": self.label,
            "strategy": self.strategy,
            "params": dict(self.params),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "data_range": list(self.data_range),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "gates": dict(self.gates),
            "provenance": dict(self.provenance),
        }


def read_research_report(path: str | Path) -> dict[str, Any]:
    """Load a research report. Missing/!json files are an explicit error, never silently empty."""
    report_path = Path(path)
    if not report_path.exists():
        raise FileNotFoundError(f"research report not found: {report_path}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"research report is not an object: {report_path}")
    return payload


def _report_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_fields() -> tuple[str, ...]:
    return (
        "is_trades",
        "is_win_rate",
        "is_profit_factor",
        "is_expectancy",
        "is_max_drawdown_pct",
        "oos_trades",
        "oos_win_rate",
        "oos_profit_factor",
        "oos_expectancy",
        "oos_return_pct",
        "oos_max_drawdown_pct",
    )


def _gate_fields() -> tuple[str, ...]:
    return (
        "validation_passed",
        "rejection_reasons",
        "wf_pass_rate",
        "wf_mean_pf",
        "mc_pct_profitable",
        "mc_median_dd_pct",
        "mc_p95_dd_pct",
        "spread_stable",
        "param_stable",
    )


def build_proposal(
    candidate: dict[str, Any],
    *,
    index: int,
    provenance: dict[str, Any],
    symbol: str | None = None,
    timeframe: str | None = None,
) -> PromotionProposal:
    """Turn one report candidate into a proposal, deciding eligibility on the report's own gates."""
    from mt5_platform.strategy import create_strategy

    label = str(candidate.get("name") or f"candidate_{index}")
    strategy = str(candidate.get("strategy") or "") or (strategy_for_label(label) or "")
    params = dict(candidate.get("params") or {})
    candidate_symbol = str(candidate.get("symbol") or "")
    candidate_timeframe = str(candidate.get("timeframe") or "")
    reasons: list[str] = []
    if candidate.get("validation_passed") is not True:
        reasons.append("research_validation_failed")
    if candidate.get("rejection_reasons"):
        reasons.extend(
            f"rejection_reason:{reason}" for reason in candidate.get("rejection_reasons") or []
        )
    if not strategy:
        reasons.append("unknown_strategy_for_label")
    else:
        try:
            create_strategy(strategy, **params)
        except Exception as exc:  # noqa: BLE001 - any construction failure disqualifies it
            reasons.append(f"invalid_params:{type(exc).__name__}:{exc}")
    if symbol and candidate_symbol and candidate_symbol != symbol:
        reasons.append(f"symbol_mismatch:{candidate_symbol}!={symbol}")
    if timeframe and candidate_timeframe and candidate_timeframe != timeframe:
        reasons.append(f"timeframe_mismatch:{candidate_timeframe}!={timeframe}")

    data_range = candidate.get("data_range") or ["", ""]
    return PromotionProposal(
        proposal_id=f"promo_{index}_{hashlib.sha256(label.encode('utf-8')).hexdigest()[:8]}",
        label=label,
        strategy=strategy,
        params=params,
        symbol=candidate_symbol,
        timeframe=candidate_timeframe,
        data_range=(str(data_range[0]), str(data_range[1])),
        eligible=not reasons,
        reasons=reasons,
        metrics={key: candidate.get(key) for key in _metric_fields() if key in candidate},
        gates={key: candidate.get(key) for key in _gate_fields() if key in candidate},
        provenance={**provenance, "candidate_index": index},
    )


def find_candidates(
    report_path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[PromotionProposal]:
    """Every candidate in a report, each marked eligible or not (nothing is filtered away)."""
    path = Path(report_path)
    report = read_research_report(path)
    provenance = {
        "report_path": str(path),
        "report_sha256": _report_digest(path),
        "report_generated_at": report.get("generated_at"),
        "report_symbol": report.get("symbol"),
        "report_timeframe": report.get("timeframe"),
    }
    return [
        build_proposal(
            candidate,
            index=index,
            provenance=provenance,
            symbol=symbol,
            timeframe=timeframe,
        )
        for index, candidate in enumerate(report.get("candidates") or [])
    ]


def eligible_candidates(
    report_path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[PromotionProposal]:
    return [
        proposal
        for proposal in find_candidates(report_path, symbol=symbol, timeframe=timeframe)
        if proposal.eligible
    ]


def read_active_config(path: str | Path) -> dict[str, Any]:
    """The currently approved promotions (empty structure when the file does not exist yet)."""
    config_path = Path(path)
    if not config_path.exists():
        return {"promotions": []}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"promotions": []}
    if not isinstance(payload, dict):
        return {"promotions": []}
    payload.setdefault("promotions", [])
    return payload


def approve_proposal(
    proposal: PromotionProposal,
    *,
    config_path: str | Path,
    approved_by: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one approved promotion (refuses anything the research did not validate).

    Returns the entry that was written. The runtime only uses it when it is pointed at this file,
    and the version id is stamped onto the strategy so every outcome traces back to this decision.
    """
    if not proposal.eligible:
        raise ValueError(f"candidate not eligible for promotion: {proposal.reasons}")
    if not approved_by.strip():
        raise ValueError("approved_by is required: a promotion must name a human approver")
    path = Path(config_path)
    payload = read_active_config(path)
    entry = {
        "version_id": f"cfg_{new_correlation_id()[:12]}",
        "strategy": proposal.strategy,
        "params": dict(proposal.params),
        "symbol": proposal.symbol,
        "timeframe": proposal.timeframe,
        "label": proposal.label,
        "approved_by": approved_by.strip(),
        "approved_at": (now or datetime.now(UTC)).isoformat(),
        "note": note,
        "data_range": list(proposal.data_range),
        "metrics": dict(proposal.metrics),
        "gates": dict(proposal.gates),
        "provenance": dict(proposal.provenance),
    }
    payload["promotions"] = [
        existing
        for existing in payload.get("promotions", [])
        if not (
            existing.get("strategy") == entry["strategy"]
            and existing.get("params") == entry["params"]
            and existing.get("symbol") == entry["symbol"]
            and existing.get("timeframe") == entry["timeframe"]
        )
    ]
    payload["promotions"].append(entry)
    payload["updated_at"] = entry["approved_at"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return entry


def promoted_strategies(
    config_path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[dict[str, Any]]:
    """Promotions that apply to this symbol/timeframe, in declaration order."""
    rows: list[dict[str, Any]] = []
    for entry in read_active_config(config_path).get("promotions", []):
        if symbol and entry.get("symbol") and entry["symbol"] != symbol:
            continue
        if timeframe and entry.get("timeframe") and entry["timeframe"] != timeframe:
            continue
        rows.append(entry)
    return rows


def build_promoted_strategies(
    config_path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[Any]:
    """Instantiate the approved strategies, stamping each with its promotion version id."""
    from mt5_platform.strategy import create_strategy

    built: list[Any] = []
    for entry in promoted_strategies(config_path, symbol=symbol, timeframe=timeframe):
        try:
            strategy = create_strategy(entry["strategy"], **dict(entry.get("params") or {}))
        except Exception:  # noqa: BLE001 - an unusable promotion must not stop the runtime
            continue
        strategy.version = str(entry.get("version_id") or strategy.version)
        strategy.promotion = dict(entry)
        built.append(strategy)
    return built


__all__ = [
    "PromotionProposal",
    "approve_proposal",
    "build_promoted_strategies",
    "build_proposal",
    "eligible_candidates",
    "find_candidates",
    "promoted_strategies",
    "read_active_config",
    "read_research_report",
    "strategy_for_label",
]
