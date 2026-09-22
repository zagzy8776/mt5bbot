"""Phase 2: candle-shape features (deterministic, no lookahead, explicit unknowns)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mt5_platform.historical.features import (
    CANDLE_FEATURE_VERSION,
    compute_candle_shape,
)
from mt5_platform.historical.models import HistoricalOutcome, SetupFeatures
from mt5_platform.outcomes.recorder import build_setup_features

T0 = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 100.0


def _candle(
    open_: float, high: float, low: float, close: float, volume: float = 100.0, minute: int = 0
) -> Candle:
    return Candle(T0.replace(minute=minute), open_, high, low, close, volume)


def _flat_window(n: int = 5, *, span: float = 1.0, volume: float = 100.0) -> list[Candle]:
    """A quiet window: every candle spans `span` and closes mid-range."""
    return [
        _candle(100.0, 100.0 + span / 2, 100.0 - span / 2, 100.0, volume, minute=i)
        for i in range(n)
    ]


# --------------------------------------------------------------- shape arithmetic


def test_bullish_marubozu_shape() -> None:
    features = compute_candle_shape([*_flat_window(3), _candle(100.0, 105.0, 100.0, 105.0)])
    assert features is not None
    assert features.direction == "bull"
    assert features.pattern == "marubozu"  # full body, no wicks, range above the median
    assert features.body_ratio == 1.0
    assert features.upper_wick_ratio == 0.0
    assert features.lower_wick_ratio == 0.0
    assert features.close_position_in_range == 1.0


def test_doji_shape_is_reported_as_such() -> None:
    features = compute_candle_shape([*_flat_window(3), _candle(100.0, 101.0, 99.0, 100.0)])
    assert features is not None
    assert features.pattern == "doji"
    assert features.body_ratio == 0.0
    assert features.upper_wick_ratio == 0.5
    assert features.lower_wick_ratio == 0.5


def test_hammer_and_shooting_star_are_distinguished() -> None:
    hammer = compute_candle_shape([*_flat_window(3), _candle(100.0, 100.2, 95.0, 100.1)])
    shooting = compute_candle_shape([*_flat_window(3), _candle(100.0, 105.0, 99.9, 100.1)])
    assert hammer is not None and hammer.pattern == "hammer"
    assert hammer.lower_wick_ratio > 0.9 and hammer.upper_wick_ratio < 0.1
    assert shooting is not None and shooting.pattern == "shooting_star"
    assert shooting.upper_wick_ratio > 0.9 and shooting.lower_wick_ratio < 0.1


def test_close_position_and_window_extremes() -> None:
    window = [
        _candle(100.0, 101.0, 99.0, 100.5, minute=0),
        _candle(100.5, 103.0, 99.5, 100.0, minute=1),
        _candle(100.0, 102.0, 98.0, 98.5, minute=2),
    ]
    features = compute_candle_shape(window)
    assert features is not None
    assert features.window_high == 103.0
    assert features.window_low == 98.0
    assert features.close_position_in_range == 0.125  # closed near the low of a 4-wide range
    assert features.direction == "bear"


def test_range_volume_gap_and_consecutive_direction() -> None:
    window = [
        _candle(100.0, 100.5, 99.5, 100.0, volume=100.0, minute=0),
        _candle(100.0, 100.5, 99.5, 100.0, volume=100.0, minute=1),
        _candle(102.0, 104.5, 101.0, 104.0, volume=300.0, minute=2),
    ]
    features = compute_candle_shape(window)
    assert features is not None
    assert features.range_vs_window_median is not None
    assert features.range_vs_window_median > 2.4  # a 3.5 span against a 1.0 median
    assert features.volume_ratio == 3.0
    assert features.gap_pct is not None and features.gap_pct > 1.9  # gapped up from 100 to 102
    assert features.consecutive_same_direction == 1  # the previous candle was flat


def test_consecutive_same_direction_counts_a_real_run() -> None:
    window = [
        _candle(100.0, 101.0, 99.5, 100.5, minute=0),
        _candle(100.5, 102.0, 100.0, 101.5, minute=1),
        _candle(101.5, 103.0, 101.0, 102.5, minute=2),
    ]
    features = compute_candle_shape(window)
    assert features is not None
    assert features.direction == "bull"
    assert features.consecutive_same_direction == 3


def test_window_is_bounded_and_versioned() -> None:
    window = [_candle(100.0, 101.0, 99.0, 100.5, minute=i % 60) for i in range(50)]
    features = compute_candle_shape(window, window=10)
    assert features is not None
    assert features.candles_used == 10
    assert features.version == CANDLE_FEATURE_VERSION
    assert features.to_dict()["version"] == CANDLE_FEATURE_VERSION


# ------------------------------------------------------------- explicit unknowns


def test_no_candles_means_no_features_not_invented_ones() -> None:
    assert compute_candle_shape(None) is None
    assert compute_candle_shape([]) is None


def test_unusable_candles_are_ignored() -> None:
    assert compute_candle_shape([{"open": None, "high": None, "low": None, "close": None}]) is None
    assert compute_candle_shape([object()]) is None
    # a usable candle after an unusable one still produces features
    features = compute_candle_shape([object(), _candle(100.0, 101.0, 99.0, 100.5)])
    assert features is not None and features.candles_used == 1


def test_missing_volume_and_previous_close_stay_none() -> None:
    single = compute_candle_shape([_candle(100.0, 101.0, 99.0, 100.5, volume=0.0)])
    assert single is not None
    assert single.volume_ratio is None  # no usable volume history
    assert single.gap_pct is None  # no previous close
    assert single.range_vs_window_median is None  # no previous range
    assert single.consecutive_same_direction == 1


def test_flat_candle_span_is_handled() -> None:
    features = compute_candle_shape([_candle(100.0, 100.0, 100.0, 100.0)])
    assert features is not None
    assert features.direction == "flat"
    assert features.pattern == "doji"
    assert features.body_ratio == 0.0
    assert features.close_position_in_range == 0.5


# ----------------------------------------------- snapshot in the entry knowledge


def test_build_setup_features_captures_the_candle_snapshot() -> None:
    window = [*_flat_window(4), _candle(100.0, 105.0, 100.0, 105.0)]
    features = build_setup_features(
        symbol="XAUUSDm", when=T0, timeframe="M15", strategy="breakout", candles=window
    )
    assert features.candle_features["candles_used"] == 5
    assert features.candle_features["direction"] == "bull"
    assert features.candle_features["pattern"] == "marubozu"
    assert features.strategy == "breakout"


def test_build_setup_features_without_candles_says_nothing() -> None:
    features = build_setup_features(symbol="XAUUSDm", when=T0)
    assert features.candle_features == {}  # explicit absence, not zeroes


def test_candle_snapshot_is_frozen_with_the_outcome() -> None:
    window = [*_flat_window(4), _candle(100.0, 105.0, 100.0, 105.0)]
    outcome = HistoricalOutcome(
        instrument="XAUUSDm",
        direction="buy",  # type: ignore[arg-type]
        timestamp=T0,
        entry=105.0,
        features=SetupFeatures(
            instrument="XAUUSDm",
            timestamp=T0,
            candle_features={"version": CANDLE_FEATURE_VERSION, "pattern": "marubozu"},
        ),
    )
    dumped = outcome.model_dump(mode="json")
    assert dumped["features"]["candle_features"]["pattern"] == "marubozu"
    # the version travels with the record, so later feature changes cannot silently reinterpret it
    assert dumped["features"]["candle_features"]["version"] == CANDLE_FEATURE_VERSION
    assert compute_candle_shape(window) is not None
