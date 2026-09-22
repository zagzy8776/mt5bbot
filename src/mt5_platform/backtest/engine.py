"""Bar-based backtest engine.

Design rules (each one removes a classic way backtests lie):

* No lookahead: a signal is generated from bar *i*'s close and filled at bar *i+1*'s open.
* Costs are real: BUY fills at ask (bid + spread), exits cross the spread, adverse slippage
  on entries and stop-outs, commission per lot.
* SL-first: if SL and TP are both inside one bar, the stop is assumed to hit first.
* Gaps: a stop that gaps is filled at the gap price, not at your stop.
* Sizing is the live sizing (`position_size_for_risk`): trades the live bot would refuse for
  being too big for the account are skipped and counted, not quietly taken at 0.01 lot.
* The same account rules apply: max positions, exposure cap, daily-loss stop, drawdown halt.

Not modelled: margin/stop-out, swaps, requotes, news spikes inside a bar, tick-level fills.
Treat results as an optimistic upper bound on live performance.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from mt5_platform.backtest.data import Bar, bar_to_event
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.instruments import DEFAULT_SPECS, InstrumentSpec, position_size_for_risk
from mt5_platform.strategy.base import Strategy


@dataclass
class BacktestConfig:
    symbol: str = "XAUUSD"
    spec: InstrumentSpec | None = None
    starting_balance: float = 100.0
    risk_pct: float = 1.0
    max_volume: float = 0.10
    max_positions: int = 3
    max_exposure_pct: float = 300.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    default_spread_price: float = 0.30
    slippage_price: float = 0.05
    commission_per_lot: float = 0.0

    def resolved_spec(self) -> InstrumentSpec:
        # Case-insensitive lookup: DEFAULT_SPECS keys are upper-case reference names,
        # but broker symbols must keep their exact case (XAUUSDm != XAUUSDM).
        spec = self.spec
        if spec is None:
            key = self.symbol.strip().lower()
            for name, candidate in DEFAULT_SPECS.items():
                if name.lower() == key:
                    spec = candidate
                    break
        if spec is None:
            raise ValueError(f"no instrument spec for {self.symbol}; pass BacktestConfig(spec=...)")
        return spec


@dataclass
class Trade:
    strategy: str
    side: OrderSide
    volume: float
    entry: float
    exit: float
    stop_loss: float
    opened_at: datetime
    closed_at: datetime
    pnl: float
    r_multiple: float
    exit_reason: str  # sl | tp | end


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade]
    equity_curve: list[tuple[datetime, float]]
    skips: Counter
    bars_used: int
    halted: bool = False
    halt_reason: str | None = None
    ruined: bool = False

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.config.starting_balance


@dataclass
class _Pos:
    strategy: str
    side: OrderSide
    volume: float
    entry: float
    sl: float
    tp: float | None
    opened_at: datetime
    risk_money: float


def _exit_price(pos: _Pos, bar: Bar, spread: float, slip: float) -> tuple[float, str] | None:
    if pos.side is OrderSide.BUY:  # bars are bid; long exits at bid
        if bar.low <= pos.sl:
            return min(pos.sl, bar.open) - slip, "sl"
        if pos.tp is not None and bar.high >= pos.tp:
            return max(pos.tp, bar.open), "tp"
    else:  # short exits at ask = bid + spread
        if bar.high + spread >= pos.sl:
            return max(pos.sl, bar.open + spread) + slip, "sl"
        if pos.tp is not None and bar.low + spread <= pos.tp:
            return min(pos.tp, bar.open + spread), "tp"
    return None


def run_backtest(
    bars: list[Bar], strategies: list[Strategy], config: BacktestConfig
) -> BacktestResult:
    spec = config.resolved_spec()
    vpp = spec.value_per_price_unit
    for s in strategies:
        s.reset()

    realized = config.starting_balance
    open_pos: list[_Pos] = []
    trades: list[Trade] = []
    curve: list[tuple[datetime, float]] = []
    skips: Counter = Counter()
    pending = []
    peak = realized
    halted, halt_reason, ruined = False, None, False
    day = None
    day_start_equity = realized
    day_stopped = False
    equity = realized

    def close(pos: _Pos, px: float, when: datetime, reason: str) -> None:
        nonlocal realized
        direction = 1.0 if pos.side is OrderSide.BUY else -1.0
        pnl = (
            direction * (px - pos.entry) * vpp * pos.volume - config.commission_per_lot * pos.volume
        )
        realized += pnl
        trades.append(
            Trade(
                pos.strategy,
                pos.side,
                pos.volume,
                pos.entry,
                px,
                pos.sl,
                pos.opened_at,
                when,
                pnl,
                pnl / pos.risk_money if pos.risk_money > 0 else 0.0,
                reason,
            )
        )

    for bar in bars:
        spread = bar.spread * spec.tick_size if bar.spread > 0 else config.default_spread_price
        slip = config.slippage_price

        if bar.time.date() != day:
            day = bar.time.date()
            day_start_equity = equity
            day_stopped = False

        # (a) fill last bar's signals at this bar's open
        for sig in pending:
            if halted:
                skips["halted"] += 1
                continue
            if day_stopped:
                skips["daily_loss_limit"] += 1
                continue
            if len(open_pos) >= config.max_positions:
                skips["max_positions"] += 1
                continue
            if any(p.side is sig.direction for p in open_pos):
                skips["duplicate_direction"] += 1
                continue
            if sig.stop_loss is None:
                skips["no_stop_loss"] += 1
                continue
            buy = sig.direction is OrderSide.BUY
            fill = bar.open + spread + slip if buy else bar.open - slip
            if (buy and sig.stop_loss >= fill) or (not buy and sig.stop_loss <= fill):
                skips["stop_wrong_side"] += 1
                continue
            volume = position_size_for_risk(
                equity=equity,
                risk_pct=config.risk_pct,
                entry=fill,
                stop=sig.stop_loss,
                spec=spec,
                max_volume=config.max_volume,
            )
            if volume <= 0:
                skips["size_too_small"] += 1
                continue
            exposure = sum(spec.notional(p.entry, p.volume) for p in open_pos)
            if (exposure + spec.notional(fill, volume)) / equity * 100.0 > config.max_exposure_pct:
                skips["max_exposure"] += 1
                continue
            open_pos.append(
                _Pos(
                    sig.strategy_name,
                    sig.direction,
                    volume,
                    fill,
                    sig.stop_loss,
                    sig.take_profit,
                    bar.time,
                    spec.risk_money(fill, sig.stop_loss, volume),
                )
            )
        pending = []

        # (b) exits inside this bar (including positions opened at this bar's open)
        for pos in list(open_pos):
            hit = _exit_price(pos, bar, spread, slip)
            if hit:
                close(pos, hit[0], bar.time, hit[1])
                open_pos.remove(pos)

        # (c) mark to market at the close
        floating = 0.0
        for pos in open_pos:
            mark = bar.close if pos.side is OrderSide.BUY else bar.close + spread
            direction = 1.0 if pos.side is OrderSide.BUY else -1.0
            floating += direction * (mark - pos.entry) * vpp * pos.volume
        equity = realized + floating
        curve.append((bar.time, equity))
        peak = max(peak, equity)

        if equity <= 0:
            ruined = True
            for pos in list(open_pos):
                close(pos, bar.close, bar.time, "end")
            open_pos.clear()
            break
        if day_start_equity > 0 and (day_start_equity - equity) / day_start_equity * 100 >= (
            config.max_daily_loss_pct
        ):
            day_stopped = True
        if not halted and (peak - equity) / peak * 100 >= config.max_drawdown_pct:
            halted, halt_reason = True, f"drawdown >= {config.max_drawdown_pct}%"

        # (d) signals from this bar's close -> filled at the NEXT bar's open
        event = bar_to_event(bar, config.symbol, spread)
        for strat in strategies:
            if strat.enabled and strat.handles(config.symbol):
                sig = strat.generate_signal(event)
                if sig is not None:
                    pending.append(sig)

    if open_pos and bars:
        last = bars[-1]
        spread = last.spread * spec.tick_size if last.spread > 0 else config.default_spread_price
        for pos in list(open_pos):
            px = last.close if pos.side is OrderSide.BUY else last.close + spread
            close(pos, px, last.time, "end")
        curve[-1] = (last.time, realized)

    return BacktestResult(
        config=config,
        trades=trades,
        equity_curve=curve,
        skips=skips,
        bars_used=len(bars),
        halted=halted,
        halt_reason=halt_reason,
        ruined=ruined,
    )
