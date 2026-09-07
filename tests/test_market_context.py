"""Phase A — Market Context Engine + regime classification.

Boundary tests: ambiguous conditions, data-quality degradation, breakouts that
fail, volatility spikes mid-trend, staleness, malformed feeds. Deterministic.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.common.enums import DataQualityLevel, RegimeLabel
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.context import (
    MarketContext,
    MarketContextEngine,
    RegimeClassifier,
    TimeframeCandleBuilder,
)
from mt5_platform.context.features import (
    compute_breakout,
    compute_structure,
    compute_support_resistance,
    compute_trend,
    compute_volatility,
)
from mt5_platform.context.models import Candle, DataQuality

BASE = datetime(2025, 6, 2, 9, 0, tzinfo=UTC)  # a Monday


def make_event(
    ts: datetime,
    price: float,
    *,
    bid: float | None = None,
    ask: float | None = None,
    volume: float = 1.0,
) -> MarketDataEvent:
    return MarketDataEvent(
        timestamp=ts,
        source="test",
        symbol="XAUUSD",
        bid=bid,
        ask=ask,
        price=price,
        volume=volume,
    )


def make_engine(**kwargs) -> MarketContextEngine:
    defaults = dict(
        symbol="XAUUSD",
        timeframes_s=(60, 300, 900, 3600, 14400),
        primary_timeframe_s=60,
        min_primary_bars=30,
    )
    defaults.update(kwargs)
    return MarketContextEngine(**defaults)


def feed(
    engine: MarketContextEngine,
    prices: list[float],
    *,
    start: datetime = BASE,
    interval_s: float = 60.0,
    spread: float | None = 0.5,
    minutes: list[int] | None = None,
) -> None:
    """Feed one tick per interval (or per explicit minute offset)."""
    for i, price in enumerate(prices):
        if minutes is not None:
            ts = start + timedelta(minutes=minutes[i])
        else:
            ts = start + timedelta(seconds=i * interval_s)
        if spread is not None:
            event = make_event(ts, price, bid=price - spread / 2, ask=price + spread / 2)
        else:
            event = make_event(ts, price)
        engine.update(event)


def uptrend(n: int = 120, base: float = 2500.0, step: float = 0.5) -> list[float]:
    """Rising series with realistic pullbacks (a pure ramp has no swings)."""
    prices = [base]
    p = base
    for i in range(1, n):
        p += -0.6 if i % 5 == 0 else step
        prices.append(p)
    return prices


def downtrend(n: int = 120, base: float = 2600.0, step: float = 0.5) -> list[float]:
    prices = [base]
    p = base
    for i in range(1, n):
        p += 0.6 if i % 5 == 0 else -step
        prices.append(p)
    return prices


def sine_range(n: int = 120, base: float = 2500.0, amp: float = 3.0, period: float = 5.0):
    return [base + amp * math.sin(i / period) for i in range(n)]


# ---------------------------------------------------------------------------
# Candle builders
# ---------------------------------------------------------------------------


def test_candle_builder_buckets_and_ohlc() -> None:
    b = TimeframeCandleBuilder(60)
    t0 = BASE
    b.update(t0, 2500.0)
    b.update(t0 + timedelta(seconds=10), 2502.0)
    b.update(t0 + timedelta(seconds=30), 2499.0)
    b.update(t0 + timedelta(seconds=61), 2505.0)  # next bucket closes the first
    candles = b.candles()
    assert len(candles) == 1
    c = candles[0]
    assert c.open == 2500.0
    assert c.high == 2502.0
    assert c.low == 2499.0
    assert c.close == 2499.0
    assert c.tick_count == 3


def test_multi_timeframe_from_same_stream() -> None:
    engine = make_engine()
    feed(engine, uptrend(120))
    ctx = engine.build_context()
    assert ctx is not None
    m1 = ctx.candles["M1"]
    m5 = ctx.candles["M5"]
    assert len(m1) == 119  # last candle still forming
    assert len(m5) == 23
    # every M5 candle aggregates 5 M1 ticks
    assert all(c.tick_count == 5 for c in m5)
    # M5 high covers its constituent prices
    prices = uptrend(120)
    assert m5[0].high == max(prices[:5])


# ---------------------------------------------------------------------------
# Context fundamentals
# ---------------------------------------------------------------------------


def test_context_fields_and_ids() -> None:
    engine = make_engine()
    feed(engine, uptrend(120))
    ctx = engine.build_context()
    assert ctx is not None
    assert isinstance(ctx, MarketContext)
    assert ctx.context_id.startswith("ctx_")
    assert ctx.instrument == "XAUUSD"
    assert ctx.current_price == pytest.approx(uptrend(120)[-1])
    assert ctx.bid is not None and ctx.ask is not None
    assert ctx.timestamp == BASE + timedelta(seconds=119 * 60)
    assert ctx.session.utc_hour == 10  # 09:00 + 119 minutes
    ctx2 = engine.build_context()
    assert ctx2 is not None and ctx2.context_id != ctx.context_id


def test_empty_engine_returns_none() -> None:
    assert make_engine().build_context() is None


def test_engine_rejects_bad_primary_timeframe() -> None:
    with pytest.raises(ValueError):
        make_engine(primary_timeframe_s=777)


# ---------------------------------------------------------------------------
# Regime classification — happy paths with evidence
# ---------------------------------------------------------------------------


def test_uptrend_is_trending_with_evidence() -> None:
    engine = make_engine()
    feed(engine, uptrend())
    ctx = engine.build_context()
    assert ctx is not None and ctx.regime is RegimeLabel.TRENDING
    assert ctx.regime_confidence is not None and ctx.regime_confidence >= 0.7
    ev = ctx.regime_evidence
    assert ev["reason"] == "directional_persistence"
    assert ev["efficiency_ratio"] > 0.4
    assert ev["slope_per_bar_pct"] > 0
    assert ctx.trend is not None and ctx.trend.structure_score > 0.5
    assert ctx.structure is not None and ctx.structure.structure_trend == "up"


def test_downtrend_is_trending_with_negative_slope() -> None:
    engine = make_engine()
    feed(engine, downtrend())
    ctx = engine.build_context()
    assert ctx is not None and ctx.regime is RegimeLabel.TRENDING
    assert ctx.regime_evidence["slope_per_bar_pct"] < 0
    assert ctx.trend is not None and ctx.trend.structure_score < -0.5


def test_sine_range_is_ranging() -> None:
    engine = make_engine()
    feed(engine, sine_range())
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.regime is RegimeLabel.RANGING
    ev = ctx.regime_evidence
    assert ev["reason"] == "bounded_weak_persistence"
    assert ev["efficiency_ratio"] < 0.25
    assert ctx.trend is not None and abs(ctx.trend.structure_score) < 0.5


def test_ambiguous_efficiency_is_transition() -> None:
    # 3 up-steps then a 1.5 retrace: efficiency lands in the 0.25-0.35 no-man's land.
    prices = [2500.0]
    p = 2500.0
    for _ in range(40):
        for step in (1.0, 1.0, 1.0, -1.5):
            p += step
            prices.append(p)
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None and ctx.regime is RegimeLabel.TRANSITION
    assert ctx.regime_evidence["reason"] == "conflicting_features"


# ---------------------------------------------------------------------------
# Regime classification — boundary / adversarial cases
# ---------------------------------------------------------------------------


def test_strong_trend_plus_volatility_spike_is_not_confident_trend() -> None:
    prices = uptrend(100)
    p = prices[-1]
    for _ in range(20):
        p += 8.0 if len(prices) % 2 == 0 else -7.0
        prices.append(p)
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.regime is RegimeLabel.HIGH_VOLATILITY
    assert ctx.regime_evidence["atr_percentile"] >= 85
    assert ctx.regime is not RegimeLabel.TRENDING


def test_vol_compression_after_expansion_is_low_volatility() -> None:
    prices = sine_range(100, amp=3.0)
    p = prices[-1]
    for _ in range(20):
        p += 0.05 if len(prices) % 2 == 0 else -0.05
        prices.append(p)
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None and ctx.regime is RegimeLabel.LOW_VOLATILITY


def test_confirmed_breakout_beats_trend_and_range() -> None:
    prices = sine_range(117, amp=3.0)
    prices += [2506.0, 2509.0, 2511.0]  # 3 closes above the prior range
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.breakout.state == "confirmed"
    assert ctx.breakout.direction == "up"
    assert ctx.breakout.boundary is not None
    assert ctx.regime is RegimeLabel.BREAKOUT
    assert "breakout" in ctx.regime_evidence


def test_breakout_then_immediate_reversal_is_transition_not_breakout() -> None:
    prices = sine_range(117, amp=3.0)
    prices += [2508.0, 2502.0, 2500.0]  # pierce the top, then close back inside
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.breakout.state == "failed"
    assert ctx.regime is RegimeLabel.TRANSITION
    assert ctx.regime_evidence["reason"] == "failed_breakout"


def test_pending_breakout_is_not_yet_breakout_regime() -> None:
    # 2507 lands in a completed candle; 2508 stays forming (detection lag = 1 bar).
    prices = sine_range(118, amp=3.0)
    prices += [2507.0, 2508.0]
    engine = make_engine()
    feed(engine, prices)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.breakout.state == "pending"
    assert ctx.regime is not RegimeLabel.BREAKOUT


def test_extreme_tick_move_is_abnormal() -> None:
    engine = make_engine()
    feed(engine, uptrend(119))
    # one tick 30 dollars above the trend — far beyond ~0.5 ATR
    ts = BASE + timedelta(seconds=119 * 60)
    engine.update(make_event(ts, uptrend(119)[-1] + 30.0))
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.regime is RegimeLabel.ABNORMAL
    assert ctx.regime_evidence["reason"] == "extreme_tick_move"
    assert ctx.regime_evidence["extreme_move_atr_mult"] >= 8.0


# ---------------------------------------------------------------------------
# Data quality is part of intelligence
# ---------------------------------------------------------------------------


def test_insufficient_history_is_undefined_and_unusable() -> None:
    engine = make_engine()
    feed(engine, uptrend(10))
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.regime is RegimeLabel.UNDEFINED
    assert ctx.usable_for_trading is False
    assert ctx.data_quality.level is DataQualityLevel.DEGRADED
    assert "insufficient_history" in ctx.data_quality.issues
    assert ctx.trend is None  # features refused rather than fabricated


def test_missing_higher_timeframes_do_not_degrade_context() -> None:
    engine = make_engine()
    feed(engine, uptrend(120))  # only ~2 hours of data: H1/H4 sparse
    ctx = engine.build_context()
    assert ctx is not None
    assert "H4" in ctx.data_quality.insufficient_history_timeframes
    assert ctx.data_quality.level is DataQualityLevel.OK
    assert ctx.usable_for_trading is True  # primary timeframe is healthy


def test_scattered_m1_gaps_degrade_quality() -> None:
    engine = make_engine()
    minutes = [m for m in range(100) if m % 5 != 4]  # every 5th minute missing
    feed(engine, [2500 + 0.1 * i for i in range(len(minutes))], minutes=minutes)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.data_quality.missing_bars > 0
    assert ctx.data_quality.level is DataQualityLevel.DEGRADED
    assert "candle_gaps" in ctx.data_quality.issues


def test_large_gap_is_critical_and_blocks_trading() -> None:
    engine = make_engine()
    minutes = list(range(10)) + list(range(110, 120))
    feed(engine, [2500.0 + i for i in range(len(minutes))], minutes=minutes)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.data_quality.level is DataQualityLevel.CRITICAL
    assert ctx.usable_for_trading is False
    assert ctx.regime is RegimeLabel.ABNORMAL
    assert ctx.regime_evidence["reason"] == "data_quality_critical"


def test_stale_data_degrades_then_blocks() -> None:
    engine = make_engine()
    feed(engine, uptrend(120))
    ctx_warn = engine.build_context(now=BASE + timedelta(seconds=119 * 60 + 600))
    assert ctx_warn is not None
    assert ctx_warn.data_quality.level is DataQualityLevel.DEGRADED
    assert "stale_data_warn" in ctx_warn.data_quality.issues
    assert ctx_warn.usable_for_trading is True  # degraded, but still classifiable

    ctx_dead = engine.build_context(now=BASE + timedelta(seconds=119 * 60 + 7200))
    assert ctx_dead is not None
    assert ctx_dead.data_quality.level is DataQualityLevel.CRITICAL
    assert ctx_dead.usable_for_trading is False
    assert ctx_dead.regime is RegimeLabel.ABNORMAL


def test_malformed_and_inconsistent_ticks_are_counted_and_block() -> None:
    engine = make_engine()
    feed(engine, uptrend(40))
    ts = BASE + timedelta(seconds=40 * 60)
    for i in range(12):
        if i % 2 == 0:
            engine.update(make_event(ts + timedelta(seconds=i), -1.0))  # malformed
        else:
            engine.update(  # bid > ask
                make_event(ts + timedelta(seconds=i), 2500.0, bid=2502.0, ask=2500.0)
            )
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.data_quality.malformed_count == 6
    assert ctx.data_quality.inconsistent_count == 6
    assert ctx.data_quality.level is DataQualityLevel.CRITICAL
    assert ctx.usable_for_trading is False


def test_duplicate_tick_is_counted_not_fed() -> None:
    engine = make_engine()
    feed(engine, uptrend(40))
    dup = make_event(BASE + timedelta(seconds=39 * 60), uptrend(40)[-1])
    engine.update(dup)
    engine.update(dup)
    assert engine.tick_count == 40
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.data_quality.duplicate_count == 2


# ---------------------------------------------------------------------------
# Session + liquidity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (datetime(2025, 6, 2, 3, 0, tzinfo=UTC), "asia"),
        (datetime(2025, 6, 2, 9, 0, tzinfo=UTC), "london"),
        (datetime(2025, 6, 2, 14, 0, tzinfo=UTC), "london_ny_overlap"),
        (datetime(2025, 6, 2, 19, 0, tzinfo=UTC), "new_york"),
        (datetime(2025, 6, 7, 12, 0, tzinfo=UTC), "weekend"),
        (datetime(2025, 6, 6, 23, 0, tzinfo=UTC), "weekend"),
    ],
)
def test_session_detection(ts: datetime, expected: str) -> None:
    engine = make_engine(min_primary_bars=2)
    feed(engine, [2500.0, 2501.0], start=ts)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.session.label == expected


def test_liquidity_levels() -> None:
    engine = make_engine(min_primary_bars=2)
    feed(engine, [2500.0] * 30, spread=0.5)
    feed(
        engine,
        [2500.0],
        start=BASE + timedelta(seconds=30 * 60),
        spread=1.2,  # >= 2x median
    )
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.liquidity.level == "wide"

    engine2 = make_engine(min_primary_bars=2)
    feed(engine2, [2500.0] * 30, spread=0.5)
    feed(
        engine2,
        [2500.0],
        start=BASE + timedelta(seconds=30 * 60),
        spread=2.5,  # >= 4x median
    )
    ctx2 = engine2.build_context()
    assert ctx2 is not None
    assert ctx2.liquidity.level == "extreme"


def test_liquidity_insufficient_with_few_samples() -> None:
    engine = make_engine(min_primary_bars=2)
    feed(engine, [2500.0] * 5, spread=0.5)
    ctx = engine.build_context()
    assert ctx is not None
    assert ctx.liquidity.level == "insufficient"


# ---------------------------------------------------------------------------
# Support/resistance + feature unit checks
# ---------------------------------------------------------------------------


def test_support_resistance_clusters_near_swing_points() -> None:
    levels = [(2505.0, "resistance"), (2505.1, "resistance"), (2495.0, "support")]
    out = compute_support_resistance(levels, current_price=2500.0, atr=1.0)
    assert len(out) == 2
    assert out[0].kind == "support"  # closest first
    assert out[0].price == pytest.approx(2495.0)
    assert out[1].touches == 2


def test_features_refuse_insufficient_candles() -> None:
    short = [Candle(timestamp=BASE, open=1, high=1, low=1, close=1)] * 10
    assert compute_trend(short) is None
    assert compute_volatility(short) is None
    features, levels = compute_structure(short)
    assert features is None and levels == []
    assert compute_breakout(short, None).state == "none"


def test_breakout_needs_a_real_range() -> None:
    flat = [
        Candle(timestamp=BASE + timedelta(minutes=i), open=2500, high=2500, low=2500, close=2500)
        for i in range(30)
    ]
    assert compute_breakout(flat, None).state == "none"


# ---------------------------------------------------------------------------
# Classifier unit tests (direct) — instability and undefined
# ---------------------------------------------------------------------------


def _full_features():
    from mt5_platform.context.models import (
        MomentumFeatures,
        StructureFeatures,
        TrendFeatures,
        VolatilityFeatures,
    )

    return dict(
        trend=TrendFeatures(
            slope_per_bar_pct=0.01,
            efficiency_ratio=0.4,
            structure_score=0.3,
            net_change_pct=1.0,
        ),
        volatility=VolatilityFeatures(
            atr=1.0,
            atr_percentile=50.0,
            atr_to_median=1.0,
            range_pct=2.0,
        ),
        momentum=MomentumFeatures(roc_pct=0.5, persistence=0.7, consecutive_same_dir=3),
        structure=StructureFeatures(
            swing_highs=3,
            swing_lows=3,
            higher_highs=2,
            lower_highs=1,
            higher_lows=2,
            lower_lows=1,
            structure_trend="up",
        ),
        breakout=compute_breakout(
            [
                Candle(
                    timestamp=BASE + timedelta(minutes=i),
                    open=2500,
                    high=2502,
                    low=2498,
                    close=2500,
                )
                for i in range(30)
            ],
            1.0,
        ),
        data_quality=DataQuality(),
        extreme_move_atr_mult=None,
        classified_at=BASE,
    )


def test_classifier_undefined_without_features() -> None:
    c = RegimeClassifier()
    assessment = c.classify(
        trend=None,
        volatility=None,
        momentum=None,
        structure=None,
        breakout=compute_breakout([], None),
        data_quality=DataQuality(),
        extreme_move_atr_mult=None,
        recent_regimes=[],
        classified_at=BASE,
    )
    assert assessment.regime is RegimeLabel.UNDEFINED


def test_classifier_flags_instability_on_weak_conviction() -> None:
    c = RegimeClassifier()
    base = _full_features()
    base["trend"] = base["trend"].model_copy(update={"efficiency_ratio": 0.36})
    assessment = c.classify(
        recent_regimes=[
            RegimeLabel.TRENDING,
            RegimeLabel.RANGING,
            RegimeLabel.TRENDING,
        ],
        **base,
    )
    assert assessment.regime is RegimeLabel.TRANSITION
    assert assessment.evidence["reason"] == "regime_instability"
    assert assessment.evidence["pre_instability_regime"] == "trending"


def test_classifier_stable_conviction_survives_history() -> None:
    c = RegimeClassifier()
    base = _full_features()
    base["trend"] = base["trend"].model_copy(update={"efficiency_ratio": 0.8})
    assessment = c.classify(
        recent_regimes=[
            RegimeLabel.TRENDING,
            RegimeLabel.RANGING,
            RegimeLabel.TRENDING,
        ],
        **base,
    )
    assert assessment.regime is RegimeLabel.TRENDING


# ---------------------------------------------------------------------------
# Determinism + compatibility
# ---------------------------------------------------------------------------


def test_same_stream_is_fully_reproducible() -> None:
    e1, e2 = make_engine(), make_engine()
    feed(e1, uptrend(120))
    feed(e2, uptrend(120))
    c1, c2 = e1.build_context(), e2.build_context()
    assert c1 is not None and c2 is not None
    d1 = c1.model_dump(mode="json")
    d2 = c2.model_dump(mode="json")
    d1.pop("context_id")
    d2.pop("context_id")
    assert d1 == d2
    assert c1.context_id != c2.context_id


def test_existing_strategies_still_work_as_candidate_generators() -> None:
    from mt5_platform.strategy import SmaCrossoverStrategy

    engine = make_engine()
    feed(engine, uptrend(120))
    ctx = engine.build_context()
    assert ctx is not None
    strategy = SmaCrossoverStrategy()
    # context hook is opt-in; legacy strategies keep their tick-based path
    assert strategy.generate_from_context(ctx) is None
    event = make_event(BASE + timedelta(seconds=119 * 60), uptrend(120)[-1])
    strategy.generate_signal(event)  # must not raise
