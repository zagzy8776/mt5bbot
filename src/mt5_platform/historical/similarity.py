"""Similar-setup matching for the Historical Evidence Engine.

Given a current SetupFeatures, find historically comparable setups using
deterministic feature similarity. No embeddings, no LLM — just weighted
distance across comparable features.

Each comparable feature contributes a per-feature similarity in [0, 1],
and the overall similarity is a weighted sum normalized to [0, 1].
Missing features are treated as neutral (0.5) and their weight is
redistributed proportionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mt5_platform.historical.models import HistoricalOutcome, SetupFeatures


@dataclass(frozen=True)
class FeatureSimilarity:
    """One comparable feature axis with its weight."""

    name: str
    weight: float
    # Extractor: returns comparable float from SetupFeatures, or None if absent
    extractor_name: str = ""  # name of attribute on SetupFeatures


DEFAULT_AXES: tuple[FeatureSimilarity, ...] = (
    FeatureSimilarity("regime", 0.15, "regime"),
    FeatureSimilarity("session", 0.10, "session"),
    FeatureSimilarity("timeframe", 0.10, "timeframe"),
    FeatureSimilarity("trend_slope_pct", 0.10, "trend_slope_pct"),
    FeatureSimilarity("trend_efficiency", 0.08, "trend_efficiency"),
    FeatureSimilarity("volatility_atr_to_median", 0.10, "volatility_atr_to_median"),
    FeatureSimilarity("momentum_roc_pct", 0.10, "momentum_roc_pct"),
    FeatureSimilarity("momentum_persistence", 0.08, "momentum_persistence"),
    FeatureSimilarity("structure_trend", 0.07, "structure_trend"),
    FeatureSimilarity("range_position", 0.07, "range_position"),
    FeatureSimilarity("breakout_state", 0.05, "breakout_state"),
)


def _regime_distance(a, b) -> float:
    """Regime similarity: exact match=1.0, related=0.5, else 0.0."""
    if a is None or b is None:
        return 0.5
    if a == b:
        return 1.0
    # Trending and breakout are related
    trending_set = {"trending", "breakout"}
    if a in trending_set and b in trending_set:
        return 0.7
    # Ranging and low_volatility are related
    ranging_set = {"ranging", "low_volatility"}
    if a in ranging_set and b in ranging_set:
        return 0.7
    return 0.2


def _categorical_distance(a, b) -> float:
    if a is None or b is None or a == "" or b == "":
        return 0.5
    return 1.0 if a == b else 0.0


def _bounded_numeric_distance(a: float | None, b: float | None, span: float) -> float:
    """Similarity for a bounded numeric: 1 - |a-b|/span, clipped to [0,1]."""
    if a is None or b is None:
        return 0.5
    diff = abs(a - b)
    return max(0.0, 1.0 - diff / span)


# Per-axis span (tuned for typical XAUUSD M5 features; conservative).
_SPANS: dict[str, float] = {
    "trend_slope_pct": 0.5,       # percent per bar; 0.5% covers weak to strong
    "trend_efficiency": 1.0,      # 0..1 Kaufman ER
    "volatility_atr_to_median": 2.0,  # ratio; 2.0 covers quiet to very volatile
    "momentum_roc_pct": 2.0,     # percent; 2% covers weak to strong
    "momentum_persistence": 1.0,  # 0..1
    "range_position": 1.0,        # 0..1 already bounded
}


def _per_axis_score(axis: FeatureSimilarity, current: SetupFeatures, candidate: SetupFeatures) -> float | None:
    """Return per-axis similarity in [0,1], or None if axis not comparable."""
    a = getattr(current, axis.extractor_name, None)
    b = getattr(candidate, axis.extractor_name, None)
    if a is None or b is None:
        return None
    if axis.extractor_name == "regime":
        return _regime_distance(a, b)
    if axis.extractor_name in ("session", "timeframe", "structure_trend", "breakout_state"):
        return _categorical_distance(a, b)
    span = _SPANS.get(axis.extractor_name)
    if span is not None:
        return _bounded_numeric_distance(float(a), float(b), span)
    return None


class SimilarityScorer:
    """Deterministic similarity scorer for SetupFeatures.

    Handles missing features by treating them as neutral (0.5) and
    redistributing weight to the axes that have data.
    """

    def __init__(self, axes: tuple[FeatureSimilarity, ...] = DEFAULT_AXES) -> None:
        self.axes = axes

    def score(self, current: SetupFeatures, candidate: SetupFeatures) -> float:
        """Overall similarity in [0, 1]."""
        total_w = 0.0
        total_s = 0.0
        for axis in self.axes:
            s = _per_axis_score(axis, current, candidate)
            if s is None:
                continue
            total_s += s * axis.weight
            total_w += axis.weight
        if total_w <= 0:
            # No comparable features — neutral
            return 0.5
        # Normalize by the weight we actually used, so a partial comparison
        # still spans [0, 1] sensibly.
        return max(0.0, min(1.0, total_s / total_w))


def matches_from_outcomes(
    current: SetupFeatures,
    outcomes: list[HistoricalOutcome],
    *,
    scorer: SimilarityScorer | None = None,
    min_similarity: float = 0.5,
) -> list[tuple[HistoricalOutcome, float]]:
    """Score and filter historical outcomes against the current setup.

    Returns (outcome, similarity) pairs sorted by similarity descending.
    Outcomes without features are excluded — you cannot compare what you
    don't have a fingerprint for.
    """
    scorer = scorer or SimilarityScorer()
    scored: list[tuple[HistoricalOutcome, float]] = []
    for o in outcomes:
        if o.features is None:
            continue
        if o.instrument != current.instrument:
            continue
        s = scorer.score(current, o.features)
        if s >= min_similarity:
            scored.append((o, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
