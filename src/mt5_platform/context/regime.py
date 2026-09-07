"""Regime classification with exposed, reproducible evidence.

Precedence (each level must be backed by measurable features):
    ABNORMAL        data quality critical, or single-tick move far beyond ATR
    BREAKOUT        confirmed prior-range boundary violation
    HIGH_VOLATILITY ATR percentile >= high threshold
    LOW_VOLATILITY  ATR percentile <= low threshold
    TRANSITION      failed breakout, ambiguous features, or regime instability
    TRENDING        directional persistence + structure agreement
    RANGING         bounded movement + weak persistence
    UNDEFINED       insufficient data to compute features
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_platform.common.enums import DataQualityLevel, RegimeLabel
from mt5_platform.context.models import (
    BreakoutState,
    DataQuality,
    MomentumFeatures,
    RegimeAssessment,
    StructureFeatures,
    TrendFeatures,
    VolatilityFeatures,
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class RegimeClassifier:
    """Deterministic classifier. Same inputs -> same label, confidence, evidence."""

    def __init__(
        self,
        *,
        vol_high_ratio: float = 1.8,
        vol_low_ratio: float = 0.55,
        trend_efficiency_min: float = 0.35,
        range_efficiency_max: float = 0.25,
        structure_threshold: float = 0.2,
        extreme_move_atr: float = 8.0,
        flip_lookback: int = 3,
    ) -> None:
        self.vol_high_ratio = vol_high_ratio
        self.vol_low_ratio = vol_low_ratio
        self.trend_efficiency_min = trend_efficiency_min
        self.range_efficiency_max = range_efficiency_max
        self.structure_threshold = structure_threshold
        self.extreme_move_atr = extreme_move_atr
        self.flip_lookback = flip_lookback

    def classify(
        self,
        *,
        trend: TrendFeatures | None,
        volatility: VolatilityFeatures | None,
        momentum: MomentumFeatures | None,
        structure: StructureFeatures | None,
        breakout: BreakoutState,
        data_quality: DataQuality,
        extreme_move_atr_mult: float | None,
        recent_regimes: list[RegimeLabel],
        classified_at: datetime,
    ) -> RegimeAssessment:
        ev: dict[str, Any] = {}

        # 1. ABNORMAL — data quality critical beats everything, including
        # insufficient history: a broken feed is never classifiable.
        if data_quality.level is DataQualityLevel.CRITICAL:
            ev["reason"] = "data_quality_critical"
            ev["quality_issues"] = data_quality.issues
            return RegimeAssessment(
                regime=RegimeLabel.ABNORMAL, confidence=0.95, evidence=ev,
                classified_at=classified_at,
            )

        if (
            trend is None
            or volatility is None
            or momentum is None
            or structure is None
        ):
            return RegimeAssessment(
                regime=RegimeLabel.UNDEFINED,
                confidence=0.3,
                evidence={"reason": "insufficient_history"},
                classified_at=classified_at,
            )

        ev["efficiency_ratio"] = round(trend.efficiency_ratio or 0.0, 4)
        ev["slope_per_bar_pct"] = round(trend.slope_per_bar_pct or 0.0, 6)
        ev["structure_score"] = round(trend.structure_score or 0.0, 4)
        ev["structure_trend"] = structure.structure_trend
        ev["atr_percentile"] = (
            round(volatility.atr_percentile, 2)
            if volatility.atr_percentile is not None
            else None
        )
        ev["roc_pct"] = round(momentum.roc_pct or 0.0, 4)

        # 2. ABNORMAL — extreme single-tick move vs ATR
        if extreme_move_atr_mult is not None:
            ev["extreme_move_atr_mult"] = round(extreme_move_atr_mult, 2)
            if extreme_move_atr_mult >= self.extreme_move_atr:
                ev["reason"] = "extreme_tick_move"
                return RegimeAssessment(
                    regime=RegimeLabel.ABNORMAL, confidence=0.9, evidence=ev,
                    classified_at=classified_at,
                )

        # 3. BREAKOUT — confirmed boundary violation
        if breakout.state == "confirmed":
            conf = _clamp(
                0.65 + 0.08 * max(0, breakout.bars_outside - 1)
                - (breakout.retrace_pct or 0.0) / 200.0,
                0.6,
                0.9,
            )
            ev["breakout"] = breakout.model_dump(mode="json")
            return RegimeAssessment(
                regime=RegimeLabel.BREAKOUT, confidence=conf, evidence=ev,
                classified_at=classified_at,
            )

        # 4. TRANSITION — failed breakout (pierce + immediate reversal).
        # Checked before volatility extremes: a specific structural event
        # must not be masked by a modestly elevated ATR percentile.
        if breakout.state == "failed":
            ev["reason"] = "failed_breakout"
            ev["breakout"] = breakout.model_dump(mode="json")
            return RegimeAssessment(
                regime=RegimeLabel.TRANSITION, confidence=0.6, evidence=ev,
                classified_at=classified_at,
            )

        # 5/6. Volatility extremes override trend/range. Decided on the ATR /
        # median-ATR ratio: robust to phase alignment, unlike a raw percentile
        # of a stationary ATR series. Percentile stays in the evidence.
        ratio = volatility.atr_to_median
        ev["atr_to_median"] = (
            round(ratio, 3) if ratio is not None else None
        )
        if ratio is not None and ratio >= self.vol_high_ratio:
            conf = _clamp(
                0.6 + (ratio - self.vol_high_ratio) / self.vol_high_ratio * 0.25,
                0.6,
                0.9,
            )
            ev["reason"] = "volatility_extreme_high"
            return RegimeAssessment(
                regime=RegimeLabel.HIGH_VOLATILITY, confidence=conf, evidence=ev,
                classified_at=classified_at,
            )
        if ratio is not None and ratio <= self.vol_low_ratio:
            conf = _clamp(
                0.6 + (self.vol_low_ratio - ratio) / self.vol_low_ratio * 0.25,
                0.6,
                0.9,
            )
            ev["reason"] = "volatility_extreme_low"
            return RegimeAssessment(
                regime=RegimeLabel.LOW_VOLATILITY, confidence=conf, evidence=ev,
                classified_at=classified_at,
            )

        eff = trend.efficiency_ratio or 0.0
        struct = trend.structure_score or 0.0
        ev["trend_efficiency_min"] = self.trend_efficiency_min
        ev["range_efficiency_max"] = self.range_efficiency_max

        regime, conf = self._trend_or_range(eff, struct, ev)
        regime, conf = self._apply_instability(regime, conf, recent_regimes, ev)
        return RegimeAssessment(
            regime=regime, confidence=conf, evidence=ev,
            classified_at=classified_at,
        )

    def _trend_or_range(
        self, eff: float, struct: float, ev: dict[str, Any]
    ) -> tuple[RegimeLabel, float]:
        trending = (
            eff >= self.trend_efficiency_min
            and (abs(struct) >= self.structure_threshold or eff >= 0.5)
        )
        ranging = eff <= self.range_efficiency_max and abs(struct) < 0.3
        if trending and not ranging:
            conf = _clamp(
                0.55 + (eff - self.trend_efficiency_min) * 0.9
                + min(abs(struct), 0.6) * 0.3,
                0.5,
                0.95,
            )
            ev["reason"] = "directional_persistence"
            return RegimeLabel.TRENDING, conf
        if ranging and not trending:
            conf = _clamp(
                0.55 + (self.range_efficiency_max - eff) * 1.2
                + max(0.0, 0.3 - abs(struct)) * 0.3,
                0.5,
                0.9,
            )
            ev["reason"] = "bounded_weak_persistence"
            return RegimeLabel.RANGING, conf
        ev["reason"] = "conflicting_features"
        return RegimeLabel.TRANSITION, 0.55

    def _apply_instability(
        self,
        regime: RegimeLabel,
        conf: float,
        recent_regimes: list[RegimeLabel],
        ev: dict[str, Any],
    ) -> tuple[RegimeLabel, float]:
        """A regime flip within the lookback demotes weak convictions."""
        recent = recent_regimes[-self.flip_lookback :]
        if len(recent) < 2 or regime not in {RegimeLabel.TRENDING, RegimeLabel.RANGING}:
            return regime, conf
        flips = sum(
            1 for a, b in zip(recent, recent[1:], strict=False) if a is not b
        )
        if flips >= 2 and conf < 0.8:
            ev["reason"] = "regime_instability"
            ev["recent_regimes"] = [r.value for r in recent]
            ev["pre_instability_regime"] = regime.value
            ev["pre_instability_confidence"] = round(conf, 4)
            return RegimeLabel.TRANSITION, 0.55
        return regime, conf