"""Strategy families: the candidates the research registry needed but did not have.

Each family is a `Strategy` that emits intents only (never orders), reads nothing but the bars it
was given (no lookahead), says "not enough history" instead of guessing, and always attaches a stop
loss. They are deliberately simple and structural: reference implementations to be *tested by the
research runner*, not profitability claims.

Families: ``atr_breakout``, ``ema_adx_trend``, ``bollinger_reversion``, ``rsi_ema_pullback``,
``session_breakout``, ``mtf_trend``, ``structure_breakout``.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.indicators import (
    adx,
    aggregate_by_factor,
    atr,
    bollinger,
    ema,
    rsi,
    swing_points,
)

DISCLAIMER = "example_strategy_unvalidated"


class SeriesStrategy(Strategy):
    """Shared plumbing: bounded per-symbol OHLC series and mid-price entries."""

    series_len = 240
    _parameter_names: tuple[str, ...] = ("stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        symbols: set[str] | None = None,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
    ) -> None:
        super().__init__(symbols=symbols)
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("stop_loss_pct and take_profit_pct must be > 0")
        self.stop_loss_pct = float(stop_loss_pct)
        self.take_profit_pct = float(take_profit_pct)
        self._opens: dict[str, deque[float]] = {}
        self._highs: dict[str, deque[float]] = {}
        self._lows: dict[str, deque[float]] = {}
        self._closes: dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ plumbing

    def reset(self) -> None:
        for series in (self._opens, self._highs, self._lows, self._closes, self._volumes):
            series.clear()

    def _deque(self, store: dict[str, deque[float]], symbol: str) -> deque[float]:
        if symbol not in store:
            store[symbol] = deque(maxlen=self.series_len)
        return store[symbol]

    def _append(self, event: MarketDataEvent) -> None:
        """Record the bar's OHLC. Tick-only events contribute their price to every slot."""
        price = mid_price(event)
        if price is None:
            return
        symbol = event.symbol
        meta = event.metadata or {}
        open_p = meta.get("open")
        high = meta.get("high")
        low = meta.get("low")
        self._deque(self._opens, symbol).append(float(open_p if open_p is not None else price))
        self._deque(self._highs, symbol).append(float(high if high is not None else price))
        self._deque(self._lows, symbol).append(float(low if low is not None else price))
        self._deque(self._closes, symbol).append(float(price))
        self._deque(self._volumes, symbol).append(float(event.volume or 0.0))

    def _has_range(self, symbol: str) -> bool:
        """True when the feed is giving real bar ranges, not just prices."""
        highs = list(self._deque(self._highs, symbol))
        lows = list(self._deque(self._lows, symbol))
        if not highs:
            return False
        return any(high > low for high, low in zip(highs, lows, strict=False))

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        """Evaluate with the history of *past* bars, then record this bar exactly once.

        The order matters: a family must never see the bar it is deciding on inside its own series
        (that would be lookahead), and the bar must still be recorded even if the family raises.
        """
        if not self.handles(event.symbol):
            return None
        try:
            return self._evaluate(event)
        finally:
            self._append(event)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        """Family-specific decision using only the bars recorded so far. Default: no signal."""
        return None

    def _series(self, symbol: str) -> tuple[list[float], list[float], list[float], list[float]]:
        return (
            list(self._deque(self._opens, symbol)),
            list(self._deque(self._highs, symbol)),
            list(self._deque(self._lows, symbol)),
            list(self._deque(self._closes, symbol)),
        )

    def calculate_entry(self, event: MarketDataEvent, direction: OrderSide) -> float | None:
        return mid_price(event)

    def calculate_stop_loss(self, entry: float, direction: OrderSide) -> float | None:
        delta = entry * (self.stop_loss_pct / 100.0)
        return entry - delta if direction is OrderSide.BUY else entry + delta

    def calculate_take_profit(self, entry: float, direction: OrderSide) -> float | None:
        delta = entry * (self.take_profit_pct / 100.0)
        return entry + delta if direction is OrderSide.BUY else entry - delta

    def confidence(self, event: MarketDataEvent) -> float:
        return 0.5  # families override this with something they actually measured

    def _emit(
        self,
        event: MarketDataEvent,
        direction: OrderSide,
        *,
        reason: str,
        confidence: float,
        metadata: dict[str, Any],
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> StrategySignal | None:
        entry = self.calculate_entry(event, direction)
        if entry is None:
            return None
        stop = stop_loss if stop_loss is not None else self.calculate_stop_loss(entry, direction)
        target = (
            take_profit if take_profit is not None else self.calculate_take_profit(entry, direction)
        )
        return StrategySignal(
            symbol=event.symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            timestamp=event.timestamp,
            strategy_name=self.name,
            correlation_id=event.correlation_id,
            metadata={**metadata, "family": self.name, "disclaimer": DISCLAIMER},
        )


class AtrBreakoutStrategy(SeriesStrategy):
    """Volatility-adjusted breakout: the channel is padded by ATR and the stop scales with it."""

    name = "atr_breakout"
    description = "BUY above the lookback high plus an ATR buffer; SELL below the low minus it."
    version = "1.0.0"
    _parameter_names = ("lookback", "atr_period", "atr_buffer", "stop_atr", "target_atr")

    def __init__(
        self,
        *,
        lookback: int = 20,
        atr_period: int = 14,
        atr_buffer: float = 0.25,
        stop_atr: float = 1.5,
        target_atr: float = 3.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(symbols=symbols)
        if lookback < 2 or atr_period < 2:
            raise ValueError("lookback and atr_period must be >= 2")
        if atr_buffer < 0:
            raise ValueError("atr_buffer must be >= 0")
        if stop_atr <= 0 or target_atr <= 0:
            raise ValueError("stop_atr and target_atr must be > 0")
        self.lookback = int(lookback)
        self.atr_period = int(atr_period)
        self.atr_buffer = float(atr_buffer)
        self.stop_atr = float(stop_atr)
        self.target_atr = float(target_atr)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, highs, lows, closes = self._series(symbol)
        if len(closes) < max(self.lookback, self.atr_period) + 1 or not self._has_range(symbol):
            return None
        vol = atr(highs, lows, closes, self.atr_period)
        if vol is None or vol <= 0:
            return None
        price = mid_price(event) or closes[-1]
        channel_high = max(highs[-self.lookback :])
        channel_low = min(lows[-self.lookback :])
        upper = channel_high + self.atr_buffer * vol
        lower = channel_low - self.atr_buffer * vol
        if price > upper:
            direction = OrderSide.BUY
        elif price < lower:
            direction = OrderSide.SELL
        else:
            return None
        risk = max(vol, self.atr_buffer * vol, 1e-9) * self.stop_atr
        reward = risk * (self.target_atr / self.stop_atr)
        if direction is OrderSide.BUY:
            stop, target = price - risk, price + reward
            excess = price - upper
        else:
            stop, target = price + risk, price - reward
            excess = lower - price
        return self._emit(
            event,
            direction,
            reason=(
                f"atr_breakout_{direction.value} high={channel_high:.4f} "
                f"low={channel_low:.4f} atr={vol:.4f}"
            ),
            confidence=min(1.0, 0.5 + excess / (2.0 * vol)),
            metadata={
                "lookback": self.lookback,
                "atr": round(vol, 6),
                "channel_high": channel_high,
                "channel_low": channel_low,
                "atr_buffer": self.atr_buffer,
            },
            stop_loss=stop,
            take_profit=target,
        )


class EmaAdxTrendStrategy(SeriesStrategy):
    """Trend following: fast/slow EMA alignment gated by a minimum trend strength (ADX)."""

    name = "ema_adx_trend"
    description = "BUY when the fast EMA leads the slow EMA and ADX confirms the trend; mirrored."
    version = "1.0.0"
    _parameter_names = (
        "fast_period",
        "slow_period",
        "adx_period",
        "adx_threshold",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        fast_period: int = 12,
        slow_period: int = 26,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if fast_period < 2 or slow_period <= fast_period:
            raise ValueError("require 2 <= fast_period < slow_period")
        if adx_period < 2:
            raise ValueError("adx_period must be >= 2")
        self.fast_period = int(fast_period)
        self.slow_period = int(slow_period)
        self.adx_period = int(adx_period)
        self.adx_threshold = float(adx_threshold)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, highs, lows, closes = self._series(symbol)
        if len(closes) < self.slow_period + 1 or not self._has_range(symbol):
            return None
        fast = ema(closes, self.fast_period)
        slow = ema(closes, self.slow_period)
        strength = adx(highs, lows, closes, self.adx_period)
        price = mid_price(event) or closes[-1]
        if fast is None or slow is None or strength is None or strength < self.adx_threshold:
            return None
        if fast > slow and price > fast:
            direction = OrderSide.BUY
        elif fast < slow and price < fast:
            direction = OrderSide.SELL
        else:
            return None
        return self._emit(
            event,
            direction,
            reason=f"ema_adx_{direction.value} fast={fast:.4f} slow={slow:.4f} adx={strength:.1f}",
            confidence=min(1.0, strength / 100.0 + abs(fast - slow) / max(slow, 1e-9)),
            metadata={
                "fast_ema": round(fast, 6),
                "slow_ema": round(slow, 6),
                "adx": round(strength, 4),
                "adx_threshold": self.adx_threshold,
            },
        )


class BollingerReversionStrategy(SeriesStrategy):
    """Mean reversion at the Bollinger bands (the z-score variant remains ``mean_reversion``)."""

    name = "bollinger_reversion"
    description = "BUY a close below the lower band; SELL a close above the upper band."
    version = "1.0.0"
    _parameter_names = ("period", "deviations", "stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        period: int = 20,
        deviations: float = 2.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 0.75,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if period < 3:
            raise ValueError("period must be >= 3")
        if deviations <= 0:
            raise ValueError("deviations must be > 0")
        self.period = int(period)
        self.deviations = float(deviations)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, _highs, _lows, closes = self._series(symbol)
        if len(closes) < self.period + 1:
            return None
        bands = bollinger(closes, self.period, self.deviations)
        price = mid_price(event) or closes[-1]
        if bands is None or bands.width() <= 0:
            return None
        if price < bands.lower:
            direction = OrderSide.BUY
            stretch = (bands.lower - price) / bands.width()
        elif price > bands.upper:
            direction = OrderSide.SELL
            stretch = (price - bands.upper) / bands.width()
        else:
            return None
        return self._emit(
            event,
            direction,
            reason=(
                f"bollinger_{direction.value} close={price:.4f} "
                f"upper={bands.upper:.4f} lower={bands.lower:.4f}"
            ),
            confidence=min(1.0, 0.4 + stretch),
            metadata={
                "period": self.period,
                "deviations": self.deviations,
                "band_middle": round(bands.middle, 6),
                "band_upper": round(bands.upper, 6),
                "band_lower": round(bands.lower, 6),
                "mean_reversion_target": round(bands.middle, 6),
            },
        )


class RsiEmaPullbackStrategy(SeriesStrategy):
    """Trend plus pullback: follow the EMA trend, enter when RSI dips (or spikes) into a band."""

    name = "rsi_ema_pullback"
    description = "In an EMA uptrend, BUY when RSI pulls back below a level; SELL mirrored."
    version = "1.0.0"
    _parameter_names = (
        "trend_period",
        "rsi_period",
        "rsi_buy_level",
        "rsi_sell_level",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        trend_period: int = 50,
        rsi_period: int = 14,
        rsi_buy_level: float = 40.0,
        rsi_sell_level: float = 60.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if trend_period < 3 or rsi_period < 2:
            raise ValueError("trend_period must be >= 3 and rsi_period >= 2")
        if not 0 < rsi_buy_level < rsi_sell_level < 100:
            raise ValueError("require 0 < rsi_buy_level < rsi_sell_level < 100")
        self.trend_period = int(trend_period)
        self.rsi_period = int(rsi_period)
        self.rsi_buy_level = float(rsi_buy_level)
        self.rsi_sell_level = float(rsi_sell_level)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, _highs, _lows, closes = self._series(symbol)
        if len(closes) < self.trend_period + 2:
            return None
        trend = ema(closes[:-1], self.trend_period)
        price = mid_price(event) or closes[-1]
        momentum = rsi([*closes, price], self.rsi_period)
        if trend is None or momentum is None:
            return None
        if price > trend and momentum <= self.rsi_buy_level:
            direction = OrderSide.BUY
        elif price < trend and momentum >= self.rsi_sell_level:
            direction = OrderSide.SELL
        else:
            return None
        return self._emit(
            event,
            direction,
            reason=(
                f"rsi_ema_{direction.value} rsi={momentum:.1f} ema={trend:.4f} close={price:.4f}"
            ),
            confidence=min(1.0, 0.4 + abs(momentum - 50.0) / 50.0),
            metadata={
                "rsi": round(momentum, 4),
                "trend_ema": round(trend, 6),
                "rsi_buy_level": self.rsi_buy_level,
                "rsi_sell_level": self.rsi_sell_level,
            },
        )


class SessionBreakoutStrategy(SeriesStrategy):
    """Opening-range breakout inside a session window (session/PDH-PDL style family).

    The range is built from the first ``opening_range_bars`` bars of the session; a break of that
    range is the signal. Outside the session window nothing is evaluated, and the range resets when
    a new session day starts — so an overnight range never leaks into the next session.
    """

    name = "session_breakout"
    description = "BUY a break above the session opening range, SELL below it (session hours only)."
    version = "1.0.0"
    _parameter_names = (
        "session_start_hour",
        "session_end_hour",
        "opening_range_bars",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        session_start_hour: int = 7,
        session_end_hour: int = 16,
        opening_range_bars: int = 4,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if not 0 <= session_start_hour < 24 or not 0 <= session_end_hour <= 24:
            raise ValueError("session hours must be within 0..24")
        if session_end_hour <= session_start_hour:
            raise ValueError("session_end_hour must be after session_start_hour")
        if opening_range_bars < 1:
            raise ValueError("opening_range_bars must be >= 1")
        self.session_start_hour = int(session_start_hour)
        self.session_end_hour = int(session_end_hour)
        self.opening_range_bars = int(opening_range_bars)
        self._session_date: dict[str, Any] = {}
        self._range_high: dict[str, float] = {}
        self._range_low: dict[str, float] = {}
        self._range_bars: dict[str, int] = {}

    def reset(self) -> None:
        super().reset()
        self._session_date.clear()
        self._range_high.clear()
        self._range_low.clear()
        self._range_bars.clear()

    def _in_session(self, event: MarketDataEvent) -> bool:
        hour = event.timestamp.hour
        return self.session_start_hour <= hour < self.session_end_hour

    def _bar_range(self, event: MarketDataEvent, price: float) -> tuple[float, float]:
        """The current bar's high/low (falls back to its price for tick-only events)."""
        meta = event.metadata or {}
        high = meta.get("high")
        low = meta.get("low")
        return (
            float(high) if high is not None else price,
            float(low) if low is not None else price,
        )

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        day = event.timestamp.date()
        if self._session_date.get(symbol) != day:
            self._session_date[symbol] = day
            self._range_high.pop(symbol, None)
            self._range_low.pop(symbol, None)
            self._range_bars[symbol] = 0
        if not self._in_session(event):
            return None
        price = mid_price(event)
        if price is None:
            return None
        high, low = self._bar_range(event, price)
        bars = self._range_bars.get(symbol, 0)
        if bars < self.opening_range_bars:
            self._range_bars[symbol] = bars + 1
            self._range_high[symbol] = max(self._range_high.get(symbol, high), high)
            self._range_low[symbol] = min(self._range_low.get(symbol, low), low)
            return None
        range_high = self._range_high.get(symbol)
        range_low = self._range_low.get(symbol)
        if range_high is None or range_low is None or range_high <= range_low:
            return None
        if price > range_high:
            direction = OrderSide.BUY
        elif price < range_low:
            direction = OrderSide.SELL
        else:
            return None
        span = range_high - range_low
        excess = price - range_high if direction is OrderSide.BUY else range_low - price
        return self._emit(
            event,
            direction,
            reason=(
                f"session_breakout_{direction.value} range_high={range_high:.4f} "
                f"range_low={range_low:.4f}"
            ),
            confidence=min(1.0, 0.4 + excess / span),
            metadata={
                "session_start_hour": self.session_start_hour,
                "session_end_hour": self.session_end_hour,
                "opening_range_high": round(range_high, 6),
                "opening_range_low": round(range_low, 6),
            },
            stop_loss=range_low if direction is OrderSide.BUY else range_high,
            take_profit=(
                price + span * (self.take_profit_pct / max(self.stop_loss_pct, 1e-9))
                if direction is OrderSide.BUY
                else price - span * (self.take_profit_pct / max(self.stop_loss_pct, 1e-9))
            ),
        )


class MtfTrendStrategy(SeriesStrategy):
    """Multi-timeframe alignment: the higher timeframe must agree before a pullback entry."""

    name = "mtf_trend"
    description = "Higher-timeframe trend must agree; entry on a pullback to the LTF average."
    version = "1.0.0"
    _parameter_names = (
        "htf_factor",
        "htf_period",
        "ltf_period",
        "pullback_pct",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        htf_factor: int = 4,
        htf_period: int = 20,
        ltf_period: int = 20,
        pullback_pct: float = 0.3,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if htf_factor < 2:
            raise ValueError("htf_factor must be >= 2")
        if htf_period < 2 or ltf_period < 2:
            raise ValueError("periods must be >= 2")
        if pullback_pct < 0:
            raise ValueError("pullback_pct must be >= 0")
        self.htf_factor = int(htf_factor)
        self.htf_period = int(htf_period)
        self.ltf_period = int(ltf_period)
        self.pullback_pct = float(pullback_pct)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        opens, highs, lows, closes = self._series(symbol)
        if len(closes) < max(self.ltf_period, self.htf_period * self.htf_factor) + 1:
            return None
        _htf_opens, _htf_highs, _htf_lows, htf_closes = aggregate_by_factor(
            opens, highs, lows, closes, self.htf_factor
        )
        htf_trend = ema(htf_closes, self.htf_period)
        ltf_trend = ema(closes[:-1], self.ltf_period)
        price = mid_price(event) or closes[-1]
        if htf_trend is None or ltf_trend is None or htf_trend <= 0 or ltf_trend <= 0:
            return None
        if abs(price - ltf_trend) / ltf_trend * 100.0 > self.pullback_pct:
            return None  # extended: wait for the pullback to the lower-timeframe average
        htf_direction = "up" if htf_closes[-1] >= htf_trend else "down"
        if htf_direction == "up" and price >= ltf_trend:
            direction = OrderSide.BUY
        elif htf_direction == "down" and price <= ltf_trend:
            direction = OrderSide.SELL
        else:
            return None
        return self._emit(
            event,
            direction,
            reason=(
                f"mtf_trend_{direction.value} htf={htf_direction} htf_ema={htf_trend:.4f} "
                f"ltf_ema={ltf_trend:.4f}"
            ),
            confidence=min(1.0, 0.5 + abs(htf_closes[-1] - htf_trend) / max(htf_trend, 1e-9)),
            metadata={
                "htf_factor": self.htf_factor,
                "htf_direction": htf_direction,
                "htf_ema": round(htf_trend, 6),
                "ltf_ema": round(ltf_trend, 6),
                "pullback_pct": self.pullback_pct,
            },
        )


class StructureBreakoutStrategy(SeriesStrategy):
    """Structure break with a retest: a confirmed swing level broken *after* being retested.

    This is the deliberate difference from a plain breakout: a break that runs away from the level
    without a retest produces no signal, and the stop sits behind the structure rather than at a
    fixed percentage.
    """

    name = "structure_breakout"
    description = "BUY a break of a retested confirmed swing high; SELL a retested swing low."
    version = "1.0.0"
    _parameter_names = (
        "swing_left",
        "swing_right",
        "retest_tolerance_pct",
        "buffer_pct",
        "target_r",
    )

    def __init__(
        self,
        *,
        swing_left: int = 2,
        swing_right: int = 2,
        retest_tolerance_pct: float = 0.15,
        buffer_pct: float = 0.05,
        target_r: float = 2.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(symbols=symbols)
        if swing_left < 1 or swing_right < 1:
            raise ValueError("swing_left and swing_right must be >= 1")
        if retest_tolerance_pct < 0 or buffer_pct < 0:
            raise ValueError("tolerances must be >= 0")
        if target_r <= 0:
            raise ValueError("target_r must be > 0")
        self.swing_left = int(swing_left)
        self.swing_right = int(swing_right)
        self.retest_tolerance_pct = float(retest_tolerance_pct)
        self.buffer_pct = float(buffer_pct)
        self.target_r = float(target_r)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, highs, lows, closes = self._series(symbol)
        if len(closes) < self.swing_left + self.swing_right + 3:
            return None
        swing_highs, swing_lows = swing_points(
            highs[:-1], lows[:-1], left=self.swing_left, right=self.swing_right
        )
        price = mid_price(event) or closes[-1]
        buffer = price * (self.buffer_pct / 100.0)
        tolerance = price * (self.retest_tolerance_pct / 100.0)
        if swing_highs:
            level = swing_highs[-1][1]
            if price > level + buffer and lows[-1] <= level + tolerance:
                stop = level - buffer
                risk = max(price - stop, 1e-9)
                return self._emit(
                    event,
                    OrderSide.BUY,
                    reason=f"structure_breakout_buy level={level:.4f} close={price:.4f}",
                    confidence=min(1.0, 0.5 + (price - level) / max(tolerance, 1e-9) / 10.0),
                    metadata={
                        "swing_level": round(level, 6),
                        "swing_kind": "high",
                        "retested": True,
                        "retest_tolerance_pct": self.retest_tolerance_pct,
                    },
                    stop_loss=stop,
                    take_profit=price + risk * self.target_r,
                )
        if swing_lows:
            level = swing_lows[-1][1]
            if price < level - buffer and highs[-1] >= level - tolerance:
                stop = level + buffer
                risk = max(stop - price, 1e-9)
                return self._emit(
                    event,
                    OrderSide.SELL,
                    reason=f"structure_breakout_sell level={level:.4f} close={price:.4f}",
                    confidence=min(1.0, 0.5 + (level - price) / max(tolerance, 1e-9) / 10.0),
                    metadata={
                        "swing_level": round(level, 6),
                        "swing_kind": "low",
                        "retested": True,
                        "retest_tolerance_pct": self.retest_tolerance_pct,
                    },
                    stop_loss=stop,
                    take_profit=price - risk * self.target_r,
                )
        return None


__all__ = [
    "AtrBreakoutStrategy",
    "BollingerReversionStrategy",
    "EmaAdxTrendStrategy",
    "MtfTrendStrategy",
    "RsiEmaPullbackStrategy",
    "SeriesStrategy",
    "SessionBreakoutStrategy",
    "StructureBreakoutStrategy",
]






