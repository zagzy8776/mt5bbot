"""Attribution of recorded outcomes: which family, in which regime, is actually paying.

Coverage decisions ("run more strategies") are only meaningful if we can see *which* buckets earn.
The ledger already stores every closed trade's strategy, strategy version and the market regime at
entry, so this module groups the closed live outcomes and reports each bucket with its own sample
size and evidence grade.

Two rules are deliberate:

* the evidence thresholds are the ones the evidence engine uses — never lowered so a bucket looks
  useful, and a bucket below the weak threshold is explicitly ``insufficient``;
* a bucket that contains any backtest outcome is flagged as contaminated, because replay results are
  not forward evidence (the loader excludes them; this makes a leak visible if one ever appears).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.historical.outcome_loader import (
    DEFAULT_MIN_MODERATE,
    DEFAULT_MIN_STRONG,
    DEFAULT_MIN_WEAK,
)
from mt5_platform.historical.statistics import calculate_stats

GROUPINGS: tuple[str, ...] = ("strategy", "regime", "session", "side", "exit_cause")


def _bucket_key(outcome: HistoricalOutcome, group_by: str) -> str:
    """Grouping key for one outcome. Missing knowledge is surfaced, never guessed."""
    features = outcome.features
    if group_by == "strategy":
        return (features.strategy if features is not None and features.strategy else "unattributed")
    if group_by == "regime":
        regime = getattr(features, "regime", None) if features is not None else None
        return getattr(regime, "value", None) or "unlabelled"
    if group_by == "session":
        return (features.session or "unlabelled") if features is not None else "unlabelled"
    if group_by == "side":
        return outcome.direction.value
    if group_by == "exit_cause":
        cause = outcome.exit_cause
        return getattr(cause, "value", None) or "unknown"
    raise ValueError(f"unknown grouping: {group_by}. available={GROUPINGS}")


@dataclass
class AttributionBucket:
    """One measured bucket: a group of closed trades with its own evidence grade."""

    key: str
    group_by: str
    sample_size: int = 0
    usable: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy_pct: float = 0.0  # OutcomeStats.expectancy: mean per-trade return, in percent
    expectancy_money: float = 0.0  # mean realized P/L per closed trade, account currency
    expectancy_r: float = 0.0
    r_samples: int = 0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    share: float = 0.0  # share of all closed trades in this grouping
    evidence_quality: str = ""
    verdict: str = "insufficient_evidence"
    min_sample_weak: int = DEFAULT_MIN_WEAK
    min_sample_moderate: int = DEFAULT_MIN_MODERATE
    min_sample_strong: int = DEFAULT_MIN_STRONG
    sources: dict[str, int] = field(default_factory=dict)
    contaminated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group_by": self.group_by,
            "sample_size": self.sample_size,
            "usable": self.usable,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "expectancy_pct": self.expectancy_pct,
            "expectancy_money": self.expectancy_money,
            "expectancy_r": self.expectancy_r,
            "r_samples": self.r_samples,
            "profit_factor": self.profit_factor,
            "total_pnl": self.total_pnl,
            "share": self.share,
            "evidence_quality": self.evidence_quality,
            "verdict": self.verdict,
            "min_sample_weak": self.min_sample_weak,
            "min_sample_moderate": self.min_sample_moderate,
            "min_sample_strong": self.min_sample_strong,
            "sources": dict(self.sources),
            "contaminated": self.contaminated,
            "thresholds_lowered": False,
        }

    def is_measured(self) -> bool:
        return self.verdict == "measured"


def outcome_attribution(
    outcomes: list[HistoricalOutcome],
    *,
    group_by: str = "strategy",
    min_weak: int = DEFAULT_MIN_WEAK,
    min_moderate: int = DEFAULT_MIN_MODERATE,
    min_strong: int = DEFAULT_MIN_STRONG,
) -> list[AttributionBucket]:
    """Group closed outcomes and grade each bucket with the shared evidence thresholds.

    Ordered by sample size (largest first), so the buckets that carry information — and the ones
    that are still just noise — are both obvious. Empty/unlabelled keys are surfaced rather than
    dropped: a strategy that never attributes its trades is a data problem worth seeing.
    """
    closed = [outcome for outcome in outcomes if outcome.is_closed]
    total = len(closed)
    grouped: dict[str, list[HistoricalOutcome]] = {}
    for outcome in closed:
        grouped.setdefault(_bucket_key(outcome, group_by), []).append(outcome)

    buckets: list[AttributionBucket] = []
    for key, rows in grouped.items():
        stats = calculate_stats(
            rows, min_weak=min_weak, min_moderate=min_moderate, min_strong=min_strong
        )
        r_values = [
            value for value in (row.compute_r_multiple() for row in rows) if value is not None
        ]
        sources: dict[str, int] = {}
        for row in rows:
            source = row.source.value
            sources[source] = sources.get(source, 0) + 1
        bucket = AttributionBucket(
            key=key,
            group_by=group_by,
            sample_size=len(rows),
            usable=stats.sample_size,
            wins=stats.wins,
            losses=stats.losses,
            win_rate=stats.win_rate,
            expectancy_pct=stats.expectancy,
            expectancy_money=sum(row.realized_pnl for row in rows) / len(rows) if rows else 0.0,
            expectancy_r=sum(r_values) / len(r_values) if r_values else 0.0,
            r_samples=len(r_values),
            profit_factor=stats.profit_factor,
            total_pnl=sum(row.realized_pnl for row in rows),
            share=len(rows) / total if total else 0.0,
            evidence_quality=stats.evidence_quality.value,
            min_sample_weak=min_weak,
            min_sample_moderate=min_moderate,
            min_sample_strong=min_strong,
            sources=sources,
            contaminated="backtest" in sources,
        )
        # Below the weak threshold a bucket is measured but not concluded: no verdict is issued.
        bucket.verdict = "measured" if stats.sample_size >= min_weak else "insufficient_evidence"
        buckets.append(bucket)

    buckets.sort(key=lambda bucket: (bucket.sample_size, bucket.key), reverse=True)
    return buckets


def coverage_summary(
    outcomes: list[HistoricalOutcome], *, min_weak: int = DEFAULT_MIN_WEAK
) -> dict[str, Any]:
    """How much of the recorded record is actually interpretable right now.

    ``unmeasured`` is the honest headline: strategies that trade but have too few closed trades to
    say anything about. It is the number to watch when adding coverage — more strategies must not
    simply mean more trades.
    """
    buckets = outcome_attribution(outcomes, group_by="strategy", min_weak=min_weak)
    measured = [bucket for bucket in buckets if bucket.is_measured()]
    unattributed = next((bucket for bucket in buckets if bucket.key == "unattributed"), None)
    return {
        "strategies_recorded": len([b for b in buckets if b.key != "unattributed"]),
        "measured": len(measured),
        "unmeasured": len(buckets) - len(measured),
        "closed_trades": sum(bucket.sample_size for bucket in buckets),
        "unattributed_trades": unattributed.sample_size if unattributed else 0,
        "unmeasured_keys": sorted(b.key for b in buckets if not b.is_measured()),
        "min_sample_weak": min_weak,
        "thresholds_lowered": False,
    }


__all__ = [
    "GROUPINGS",
    "AttributionBucket",
    "coverage_summary",
    "outcome_attribution",
]
