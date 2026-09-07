"""MarketContextEngine — builds the canonical context from accepted ticks.

Deterministic: the same accepted-tick stream (and `now`) produces the same
features, data-quality assessment and regime classification.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime

from mt5_platform.common.enums import DataQualityLevel, RegimeLabel
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.context.candles import MultiTimeframeCandleBuilder
from mt5_platform.context.features import (
    compute_breakout,
    compute_momentum,
    compute_structure,
    compute_support_resistance,
    compute_trend,
    compute_volatility,
)
from mt5_platform.context.models import (
    DataQuality,
    LiquidityState,
    MarketContext,
    RegimeAssessment,
    SessionInfo,
)
from mt5_platform.context.regime import RegimeClassifier

_WEEKDAY = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _session_for(ts: datetime) -> SessionInfo:
    weekday = ts.weekday()
    hour = ts.hour
    if weekday == 5 or (weekday == 6 and hour < 22) or (weekday == 4 and hour >= 22):
        label = "weekend"
    elif hour >= 22 or hour < 7:
        label = "asia"
    elif hour < 12:
        label = "london"
    elif hour < 17:
        label = "london_ny_overlap"
    else:
        label = "new_york"
    return SessionInfo(label=label, utc_hour=hour, weekday=_WEEKDAY[weekday])


class MarketContextEngine:
    """Ingests ticks, maintains multi-timeframe candles, builds MarketContext."""

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD",
        timeframes_s: tuple[int, ...] = (60, 300, 900, 3600, 14400),
        primary_timeframe_s: int = 300,
        window_bars: int = 200,
        min_primary_bars: int = 30,
        stale_warn_s: float = 300.0,
        stale_critical_s: float = 1800.0,
        gap_warn_ratio: float = 0.10,
        gap_critical_ratio: float = 0.35,
        extreme_move_atr: float = 8.0,
    ) -> None:
        if primary_timeframe_s not in timeframes_s:
            raise ValueError("primary_timeframe_s must be one of timeframes_s")
        self.symbol = symbol.upper()
        self.primary_timeframe_s = primary_timeframe_s
        self.min_primary_bars = min_primary_bars
        self.stale_warn_s = stale_warn_s
        self.stale_critical_s = stale_critical_s
        self.gap_warn_ratio = gap_warn_ratio
        self.gap_critical_ratio = gap_critical_ratio
        self._candles = MultiTimeframeCandleBuilder(timeframes_s, max_bars=window_bars)
        self._classifier = RegimeClassifier(extreme_move_atr=extreme_move_atr)
        self._tick_count = 0
        self._duplicate_count = 0
        self._malformed_count = 0
        self._inconsistent_count = 0
        self._last_tick_ts: datetime | None = None
        self._last_price: float | None = None
        self._prev_price: float | None = None
        self._last_bid: float | None = None
        self._last_ask: float | None = None
        self._spreads: deque[float] = deque(maxlen=200)
        self._recent_regimes: deque[RegimeLabel] = deque(maxlen=10)
        self._last_assessment: RegimeAssessment | None = None

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def update(self, event: MarketDataEvent) -> bool:
        """Ingest one tick. Returns True when accepted for context building."""
        price = event.price
        if price is None and event.bid is not None and event.ask is not None:
            price = (event.bid + event.ask) / 2.0
        if price is None or not math.isfinite(price) or price <= 0:
            self._malformed_count += 1
            return False
        if event.bid is not None and event.ask is not None and event.bid > event.ask:
            self._inconsistent_count += 1
            return False
        if (
            self._last_tick_ts is not None
            and self._last_price is not None
            and event.timestamp == self._last_tick_ts
            and price == self._last_price
        ):
            self._duplicate_count += 1
            return False

        self._candles.update(event.timestamp, price, volume=event.volume or 0.0)
        self._prev_price = self._last_price
        self._last_price = price
        self._last_tick_ts = event.timestamp
        self._last_bid = event.bid
        self._last_ask = event.ask
        if event.bid is not None and event.ask is not None:
            self._spreads.append(event.ask - event.bid)
        self._tick_count += 1
        return True

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def last_assessment(self) -> RegimeAssessment | None:
        return self._last_assessment

    def _percentile(self, values: list[float], current: float) -> float | None:
        if not values:
            return None
        return sum(1 for v in values if v <= current) / len(values) * 100.0

    def _extreme_move_mult(self, atr: float | None) -> float | None:
        if atr is None or atr <= 0 or self._prev_price is None:
            return None
        return abs(self._last_price - self._prev_price) / atr

    # ------------------------------------------------------------------
    # Quality assessment
    # ------------------------------------------------------------------

    def _build_quality(
        self,
        *,
        now: datetime,
        insufficient_labels: list[str],
        primary_bars: int,
    ) -> DataQuality:
        issues: list[str] = []
        age: float | None = None
        if self._last_tick_ts is not None:
            age = (now - self._last_tick_ts).total_seconds()
            if age > self.stale_critical_s:
                issues.append("stale_data_critical")
            elif age > self.stale_warn_s:
                issues.append("stale_data_warn")
        if self._malformed_count:
            issues.append("malformed_ticks")
        if self._inconsistent_count:
            issues.append("inconsistent_quotes")
        if self._duplicate_count:
            issues.append("duplicate_ticks")
        if primary_bars < self.min_primary_bars:
            issues.append("insufficient_history")

        base_builder = self._candles.builder(min(self._candles.builders))
        missing, ratio = base_builder.gap_stats()
        if missing:
            issues.append("candle_gaps")

        level = DataQualityLevel.OK
        if (
            "stale_data_critical" in issues
            or ratio >= self.gap_critical_ratio
            or (
                self._tick_count > 0
                and (self._malformed_count + self._inconsistent_count) / max(self._tick_count, 1)
                > 0.2
            )
        ):
            level = DataQualityLevel.CRITICAL
        elif issues:
            level = DataQualityLevel.DEGRADED

        return DataQuality(
            level=level,
            last_tick_age_s=age,
            tick_count=self._tick_count,
            duplicate_count=self._duplicate_count,
            malformed_count=self._malformed_count,
            inconsistent_count=self._inconsistent_count,
            missing_bars=missing,
            missing_bar_ratio=round(ratio, 4),
            insufficient_history_timeframes=insufficient_labels,
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Context build
    # ------------------------------------------------------------------

    def build_context(self, *, now: datetime | None = None) -> MarketContext | None:
        """Build the canonical context, or None when no tick was accepted yet."""
        if self._last_tick_ts is None or self._last_price is None:
            return None
        now = now or self._last_tick_ts
        all_candles = self._candles.candles()
        primary_label = self._candles.builder(self.primary_timeframe_s).label
        primary = all_candles.get(primary_label, [])
        insufficient = self._candles.insufficient_labels(self.min_primary_bars)

        quality = self._build_quality(
            now=now,
            insufficient_labels=insufficient,
            primary_bars=len(primary),
        )

        trend = compute_trend(primary)
        volatility = compute_volatility(primary)
        momentum = compute_momentum(primary)
        structure, raw_levels = compute_structure(primary)
        if trend is not None and structure is not None:
            trend.structure_score = self._structure_score(structure)
        breakout = compute_breakout(primary, volatility.atr if volatility else None)
        s_r = compute_support_resistance(
            raw_levels, self._last_price, volatility.atr if volatility else None
        )

        assessment = self._classifier.classify(
            trend=trend,
            volatility=volatility,
            momentum=momentum,
            structure=structure,
            breakout=breakout,
            data_quality=quality,
            extreme_move_atr_mult=self._extreme_move_mult(volatility.atr if volatility else None),
            recent_regimes=list(self._recent_regimes),
            classified_at=now,
        )
        if assessment.regime is not RegimeLabel.UNDEFINED:
            self._recent_regimes.append(assessment.regime)
        self._last_assessment = assessment

        usable = quality.level is not DataQualityLevel.CRITICAL and (
            assessment.regime is not RegimeLabel.UNDEFINED
        )

        return MarketContext(
            instrument=self.symbol,
            timestamp=self._last_tick_ts,
            bid=self._last_bid,
            ask=self._last_ask,
            current_price=self._last_price,
            session=_session_for(self._last_tick_ts),
            candles=all_candles,
            trend=trend,
            volatility=volatility,
            momentum=momentum,
            structure=structure,
            support_resistance=s_r,
            breakout=breakout,
            liquidity=self._build_liquidity(),
            regime=assessment.regime,
            regime_confidence=assessment.confidence,
            regime_evidence=assessment.evidence,
            data_quality=quality,
            usable_for_trading=usable,
        )

    @staticmethod
    def _structure_score(structure) -> float:
        """[-1, 1]: +1 pure HH/HL up-structure, -1 pure LH/LL down-structure."""
        up = structure.higher_highs + structure.higher_lows
        down = structure.lower_highs + structure.lower_lows
        total = up + down
        return (up - down) / total if total > 0 else 0.0

    def _build_liquidity(self) -> LiquidityState:
        spread = None
        if self._last_bid is not None and self._last_ask is not None:
            spread = self._last_ask - self._last_bid
        samples = list(self._spreads)
        if spread is None or len(samples) < 20:
            return LiquidityState(
                current_spread=spread,
                spread_median=None,
                spread_percentile=None,
                level="insufficient" if spread is not None else None,
            )
        ordered = sorted(samples)
        median = ordered[len(ordered) // 2]
        pct = self._percentile(ordered, spread) or 0.0
        if median > 0 and spread >= 4 * median:
            level = "extreme"
        elif median > 0 and spread >= 2 * median:
            level = "wide"
        else:
            level = "normal"
        return LiquidityState(
            current_spread=spread,
            spread_median=median,
            spread_percentile=round(pct, 2),
            level=level,
        )
