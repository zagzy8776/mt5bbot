"""Phase 7: the strategy families (deterministic, no lookahead, always a stop loss)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.backtest.data import Bar, bar_to_event
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.common.instruments import DEFAULT_SPECS
from mt5_platform.strategy.indicators import (
    adx,
    aggregate_by_factor,
    atr,
    bollinger,
    ema,
    rsi,
    swing_points,
)
from mt5_platform.strategy.registry import available_strategies, create_strategy

T0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
FAMILIES = (
    "atr_breakout",
    "ema_adx_trend",
    "bollinger_reversion",
    "rsi_ema_pullback",
    "session_breakout",
    "mtf_trend",
    "structure_breakout",
)


def _bar(index: int, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        T0 + timedelta(minutes=15 * index),
        close,
        high if high is not None else close + 0.4,
        low if low is not None else close - 0.4,
        close,
        100.0,
        25.0,
    )


def _feed(
    strategy,
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
):
    """Feed a synthetic series bar by bar and return (signals, last state)."""
    signals = []
    for index, close in enumerate(closes):
        bar = _bar(
            index,
            close,
            high=highs[index] if highs else None,
            low=lows[index] if lows else None,
        )
        event = bar_to_event(bar, "XAUUSDm", 0.3)
        signal = strategy.generate_signal(event)
        if signal is not None:
            signals.append(signal)
    return signals


def _quiet_series(count: int = 60, *, start: float = 2500.0) -> list[float]:
    return [start + (0.05 if i % 2 else -0.05) for i in range(count)]


# ----------------------------------------------------------------------- indicators


def test_ema_rsi_atr_adx_bollinger_on_known_sequences() -> None:
    flat = [100.0] * 20
    assert ema(flat, 5) == pytest.approx(100.0)
    assert rsi([100.0 + i for i in range(20)], 14) == pytest.approx(100.0)
    assert rsi([100.0 - i for i in range(20)], 14) == pytest.approx(0.0)
    highs = [101.0] * 20
    lows = [99.0] * 20
    closes = [100.0] * 20
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)
    assert adx(highs, lows, closes, 14) == pytest.approx(0.0)  # no directional movement
    bands = bollinger(flat, 20)
    assert bands is not None and bands.width() == pytest.approx(0.0)

    rising = [100.0 + i for i in range(20)]
    trend = adx(rising, [v - 1 for v in rising], rising, 14)
    assert trend is not None and trend > 50.0  # clean trend reads strong
    assert ema([1.0, 2.0, 3.0], 5) is None  # not enough data is explicit


def test_swing_points_are_confirmed_not_predicted() -> None:
    highs = [10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0]
    lows = [9.0, 8.0, 7.0, 6.0, 7.0, 8.0, 9.0]
    swing_highs, swing_lows = swing_points(highs, lows, left=2, right=2)
    assert swing_highs and swing_highs[0][0] == 3  # confirmed only with 2 bars on each side
    assert swing_lows and swing_lows[0][0] == 3


def test_aggregation_uses_complete_buckets_only() -> None:
    opens = [1.0, 2.0, 3.0, 4.0, 5.0]
    highs = [2.0, 3.0, 4.0, 5.0, 6.0]
    lows = [0.5, 1.5, 2.5, 3.5, 4.5]
    closes = [1.5, 2.5, 3.5, 4.5, 5.5]
    agg_opens, agg_highs, agg_lows, agg_closes = aggregate_by_factor(
        opens, highs, lows, closes, 2
    )
    assert agg_opens == [1.0, 3.0]  # the 5th bar is not a complete bucket, so it is excluded
    assert agg_highs == [3.0, 5.0]
    assert agg_lows == [0.5, 2.5]
    assert agg_closes == [2.5, 4.5]


# ------------------------------------------------------------------------- families


def test_every_family_is_registered_and_constructible() -> None:
    names = set(available_strategies())
    for family in FAMILIES:
        assert family in names
        strategy = create_strategy(family)
        assert strategy.name == family
        assert strategy.version
        assert strategy.parameters()  # introspectable for research / promotion


def test_families_stay_silent_without_enough_history() -> None:
    for family in FAMILIES:
        strategy = create_strategy(family)
        assert _feed(strategy, [2500.0, 2500.1, 2500.2]) == [], (
            f"{family} must not trade on insufficient history"
        )


def test_atr_breakout_fires_on_an_expansion_after_a_quiet_range() -> None:
    strategy = create_strategy("atr_breakout", lookback=10, atr_period=5, atr_buffer=0.1)
    series = _quiet_series(30, start=2500.0) + [2506.0, 2507.0]
    signals = _feed(strategy, series)
    assert signals and signals[-1].direction is OrderSide.BUY
    assert signals[-1].stop_loss is not None and signals[-1].stop_loss < signals[-1].entry
    assert signals[-1].metadata["atr"] > 0


def test_ema_adx_trend_follows_a_clean_trend() -> None:
    strategy = create_strategy("ema_adx_trend", fast_period=3, slow_period=8, adx_threshold=10.0)
    rising = [2500.0 + i * 0.5 for i in range(60)]
    signals = _feed(strategy, rising)
    assert signals and all(s.direction is OrderSide.BUY for s in signals)
    assert signals[-1].metadata["adx"] >= 10.0


def test_bollinger_reversion_buys_a_stretch_below_the_band() -> None:
    strategy = create_strategy("bollinger_reversion", period=20, deviations=1.5)
    flat = [2500.0 + (0.2 if i % 2 else -0.2) for i in range(40)]
    signals = _feed(strategy, [*flat, 2490.0])
    assert signals and signals[-1].direction is OrderSide.BUY
    assert signals[-1].entry < signals[-1].metadata["band_lower"]


def test_rsi_ema_pullback_needs_both_trend_and_pullback() -> None:
    strategy = create_strategy(
        "rsi_ema_pullback", trend_period=20, rsi_period=5, rsi_buy_level=45.0
    )
    rising = [2500.0 + i * 0.4 for i in range(40)]
    assert _feed(strategy, rising) == [], "an extended trend is not a pullback entry"
    signals = _feed(strategy, [*rising, rising[-1] - 3.0])
    assert signals and signals[-1].direction is OrderSide.BUY


def test_session_breakout_breaks_the_opening_range_inside_the_session() -> None:
    strategy = create_strategy(
        "session_breakout", session_start_hour=0, session_end_hour=23, opening_range_bars=4
    )
    signals = _feed(strategy, [2500.0, 2500.5, 2501.0, 2500.8, 2502.5])
    assert signals and signals[-1].direction is OrderSide.BUY
    assert signals[-1].metadata["opening_range_high"] < signals[-1].entry
    assert signals[-1].stop_loss == pytest.approx(signals[-1].metadata["opening_range_low"])


def test_session_breakout_is_quiet_outside_its_session() -> None:
    strategy = create_strategy(
        "session_breakout", session_start_hour=5, session_end_hour=6, opening_range_bars=2
    )
    assert _feed(strategy, [2500.0 + i for i in range(10)]) == []  # bars cover 00:00-02:15 UTC


def test_mtf_trend_requires_alignment_and_a_pullback() -> None:
    strategy = create_strategy(
        "mtf_trend", htf_factor=4, htf_period=3, ltf_period=5, pullback_pct=0.5
    )
    up = _feed(strategy, [2500.0 + i * 0.3 for i in range(60)])
    assert up and all(s.direction is OrderSide.BUY for s in up)
    assert up[-1].metadata["htf_direction"] == "up"
    down = _feed(strategy, [2500.0 - i * 0.3 for i in range(60)])
    assert down and all(s.direction is OrderSide.SELL for s in down)


def test_structure_breakout_needs_a_retest_of_the_swing() -> None:
    strategy = create_strategy(
        "structure_breakout", swing_left=2, swing_right=2, retest_tolerance_pct=0.2
    )
    closes = [2500.0, 2501.0, 2502.0, 2503.0, 2502.0, 2501.0, 2502.0, 2502.6, 2504.5]
    highs = [2500.5, 2501.5, 2502.5, 2503.0, 2502.5, 2501.5, 2502.5, 2503.0, 2504.6]
    lows = [2499.5, 2500.5, 2501.5, 2502.0, 2501.0, 2500.5, 2501.5, 2502.5, 2503.5]
    signals = _feed(strategy, closes, highs=highs, lows=lows)
    assert signals and signals[-1].direction is OrderSide.BUY
    assert signals[-1].metadata["swing_kind"] == "high"
    assert signals[-1].stop_loss < signals[-1].metadata["swing_level"]


def test_families_are_deterministic_and_state_is_resettable() -> None:
    series = [2500.0 + (0.3 if i % 2 else -0.3) for i in range(30)] + [2490.0]
    first = _feed(create_strategy("bollinger_reversion", period=10, deviations=1.0), series)
    assert first, "expected at least one signal on this series"
    replay = _feed(create_strategy("bollinger_reversion", period=10, deviations=1.0), series)
    assert [s.reason for s in first] == [s.reason for s in replay]

    strategy = create_strategy("ema_adx_trend")
    _feed(strategy, series)
    strategy.reset()
    assert strategy._series("XAUUSDm")[3] == []  # noqa: SLF001 - reset clears state


def test_families_run_inside_the_backtest_engine() -> None:
    """The engine feeds bars through the same `bar_to_event` bridge the live loop uses."""
    closes = [2500.0 + (0.4 if i % 2 else -0.4) for i in range(80)]
    closes += [2500.0 + i * 0.6 for i in range(1, 40)]
    bars = [_bar(i, close, high=close + 0.6, low=close - 0.6) for i, close in enumerate(closes)]
    for family in FAMILIES:
        strategy = create_strategy(family, symbols={"XAUUSDm"})
        result = run_backtest(
            bars,
            [strategy],
            BacktestConfig(
                symbol="XAUUSDm", spec=DEFAULT_SPECS["XAUUSD"], starting_balance=10_000.0
            ),
        )
        for trade in result.trades:
            assert trade.stop_loss != trade.entry, f"{family} traded without a stop"


def test_bar_to_event_carries_the_bar_ohlc() -> None:
    event = bar_to_event(_bar(0, 2500.0, high=2501.0, low=2499.0), "XAUUSDm", 0.3)
    assert isinstance(event, MarketDataEvent)
    assert event.metadata["high"] == 2501.0 and event.metadata["low"] == 2499.0
    assert event.metadata["open"] == 2500.0 and event.metadata["close"] == 2500.0
