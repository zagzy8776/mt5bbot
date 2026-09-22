"""Scalp-shaped strategy families (research candidates, contract 8.2).

Three higher-frequency families, pre-registered in ``docs/research-contract-8.2.md`` before any run:

    scalp_micro_breakout, scalp_vwap_reversion, scalp_session_momentum

They are **candidates for measurement, not profitability claims**. Two hard constraints come from
the cost audit recorded in that contract, not from taste:

* the gold M15 spread is ~240 points median and effectively flat all day, so a sub-0.15% stop
  surrenders ~half of a 0.10R edge to cost — stops are therefore wide (0.30% / 0.50%) and targets
  are >= 1R;
* entries are on **closed** bars only (the runner replays closed bars), with no lookahead: the
  series a family sees never contains the bar it is deciding on.

Every family attaches a stop loss (mandatory downstream) and returns ``None`` instead of guessing.
"""

from __future__ import annotations

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.families import DISCLAIMER, SeriesStrategy
from mt5_platform.strategy.indicators import atr, bollinger

__all__ = [
    "ScalpMicroBreakoutStrategy",
    "ScalpSessionMomentumStrategy",
    "ScalpVwapReversionStrategy",
]


class ScalpMicroBreakoutStrategy(SeriesStrategy):
    """Breakout of the prior N-bar range, scaled by ATR, with a wide stop.

    The "scalp" part is the *frequency of entry decisions* (every closed M15 bar is tested against a
    short channel) rather than a tight stop — a tight stop is arithmetically impossible to charge
    against a 240-point spread (contract 8.2, cost ladder).
    """

    name = "scalp_micro_breakout"
    description = "BUY above the prior N-bar high (ATR-buffered); SELL below the low, wide stop."
    version = "1.0.0"
    _parameter_names = (
        "lookback",
        "atr_period",
        "atr_buffer",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        lookback: int = 20,
        atr_period: int = 14,
        atr_buffer: float = 0.25,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if lookback < 2 or atr_period < 2:
            raise ValueError("lookback and atr_period must be >= 2")
        if atr_buffer < 0:
            raise ValueError("atr_buffer must be >= 0")
        self.lookback = int(lookback)
        self.atr_period = int(atr_period)
        self.atr_buffer = float(atr_buffer)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, highs, lows, closes = self._series(symbol)
        if len(closes) < max(self.lookback, self.atr_period) + 1 or not self._has_range(symbol):
            return None
        vol = atr(highs, lows, closes, self.atr_period)
        if vol is None or vol <= 0:
            return None
        # The channel is built from bars *before* this one, so the decision cannot see itself.
        channel_high = max(highs[-self.lookback - 1 : -1])
        channel_low = min(lows[-self.lookback - 1 : -1])
        last_close = closes[-1]
        buffer = self.atr_buffer * vol
        if last_close > channel_high + buffer:
            direction = OrderSide.BUY
        elif last_close < channel_low - buffer:
            direction = OrderSide.SELL
        else:
            return None
        width = channel_high - channel_low
        extension = (
            (last_close - channel_high) / width
            if width > 0
            else 0.0
        )
        confidence = min(1.0, 0.5 + min(abs(extension), 0.5))
        return self._emit(
            event,
            direction,
            reason=(
                f"scalp_breakout_{direction.value} lookback={self.lookback} "
                f"close={last_close:.3f} channel={channel_low:.3f}..{channel_high:.3f}"
            ),
            confidence=confidence,
            metadata={
                "lookback": self.lookback,
                "atr": round(vol, 3),
                "atr_buffer": self.atr_buffer,
                "channel_high": round(channel_high, 3),
                "channel_low": round(channel_low, 3),
            },
        )


class ScalpVwapReversionStrategy(SeriesStrategy):
    """Fade a stretch away from the rolling mean (a VWAP/band reversion proxy).

    Volume-weighted price is approximated by the rolling mean of closes: the broker CSV carries tick
    volume, but a *volume-weighted* average would import a data dependency the other families do not
    have, and the contract pre-registers this as band reversion. Entries fade an extension beyond
    ``deviations`` standard deviations and target a return toward the mean.
    """

    name = "scalp_vwap_reversion"
    description = "Fade a close beyond N sigma of the rolling mean (ATR-scaled stop, >= 1R target)."
    version = "1.0.0"
    _parameter_names = (
        "window",
        "deviations",
        "atr_period",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        window: int = 20,
        deviations: float = 1.5,
        atr_period: int = 14,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if window < 5:
            raise ValueError("window must be >= 5")
        if deviations <= 0:
            raise ValueError("deviations must be > 0")
        if atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        self.window = int(window)
        self.deviations = float(deviations)
        self.atr_period = int(atr_period)

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, highs, lows, closes = self._series(symbol)
        if len(closes) < max(self.window, self.atr_period) + 1 or not self._has_range(symbol):
            return None
        bands = bollinger(closes[-self.window - 1 : -1], self.window, self.deviations)
        if bands is None or bands.middle is None:
            return None
        vol = atr(highs, lows, closes, self.atr_period)
        last_close = closes[-1]
        if last_close > bands.upper:
            direction = OrderSide.SELL  # stretched above the mean: fade it
        elif last_close < bands.lower:
            direction = OrderSide.BUY
        else:
            return None
        band_width = max(bands.upper - bands.lower, 1e-9)
        stretch = (last_close - bands.middle) / band_width
        confidence = min(1.0, 0.5 + min(abs(stretch), 0.5))
        return self._emit(
            event,
            direction,
            reason=(
                f"scalp_reversion_{direction.value} window={self.window} "
                f"close={last_close:.3f} band={bands.lower:.3f}..{bands.upper:.3f}"
            ),
            confidence=confidence,
            metadata={
                "window": self.window,
                "deviations": self.deviations,
                "band_middle": round(bands.middle, 3),
                "atr": round(vol, 3) if vol else None,
            },
        )


class ScalpSessionMomentumStrategy(SeriesStrategy):
    """Momentum inside pre-registered UTC sessions, from the cost audit (contract 8.2).

    Sessions were chosen before any result: the audit shows spread is flat all day with no
    low-spread window carrying a full sample, so the hours here are *liquid* ones rather than cheap
    ones — and the choice is fixed now so it cannot become a selection device later.
    """

    name = "scalp_session_momentum"
    description = "Inside a pre-registered UTC session, follow a short-term close-over-close push."
    version = "1.0.0"
    _parameter_names = (
        "session_start_hour",
        "session_end_hour",
        "momentum_bars",
        "min_move_pct",
        "stop_loss_pct",
        "take_profit_pct",
    )

    def __init__(
        self,
        *,
        session_start_hour: int = 7,
        session_end_hour: int = 9,
        momentum_bars: int = 4,
        min_move_pct: float = 0.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        super().__init__(
            symbols=symbols, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct
        )
        if not 0 <= session_start_hour <= 23 or not 0 <= session_end_hour <= 24:
            raise ValueError("session hours must be within 0..24")
        if session_end_hour <= session_start_hour:
            raise ValueError("session_end_hour must be after session_start_hour")
        if momentum_bars < 2:
            raise ValueError("momentum_bars must be >= 2")
        if min_move_pct < 0:
            raise ValueError("min_move_pct must be >= 0")
        self.session_start_hour = int(session_start_hour)
        self.session_end_hour = int(session_end_hour)
        self.momentum_bars = int(momentum_bars)
        self.min_move_pct = float(min_move_pct)

    def _in_session(self, event: MarketDataEvent) -> bool:
        return self.session_start_hour <= event.timestamp.hour < self.session_end_hour

    def _evaluate(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol
        _opens, _highs, _lows, closes = self._series(symbol)
        if len(closes) < self.momentum_bars + 1:
            return None
        if not self._in_session(event):
            return None
        reference = closes[-self.momentum_bars - 1]
        last_close = closes[-1]
        if reference <= 0:
            return None
        move_pct = (last_close - reference) / reference * 100.0
        if move_pct == 0 or abs(move_pct) < self.min_move_pct:
            return None
        direction = OrderSide.BUY if move_pct > 0 else OrderSide.SELL
        confidence = min(1.0, 0.5 + min(abs(move_pct), 1.0) / 2.0)
        return self._emit(
            event,
            direction,
            reason=(
                f"scalp_session_{direction.value} session={self.session_start_hour}-"
                f"{self.session_end_hour}UTC move={move_pct:+.3f}% over {self.momentum_bars} bars"
            ),
            confidence=confidence,
            metadata={
                "session_start_hour": self.session_start_hour,
                "session_end_hour": self.session_end_hour,
                "momentum_bars": self.momentum_bars,
                "move_pct": round(move_pct, 4),
                "disclaimer": DISCLAIMER,
            },
        )
