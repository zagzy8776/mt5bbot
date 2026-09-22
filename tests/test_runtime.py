"""Trading loop + MT5 feed against a fake terminal: correct sizing, fail-closed behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.data import Bar
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import MT5CandleFeed, Quote, TradingLoop
from mt5_platform.signals import SignalEngine
from mt5_platform.strategy.base import Strategy, mid_price
from tests.fake_mt5 import FakeMT5

T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)


class AlwaysBuy(Strategy):
    name = "always_buy"

    def generate_signal(self, event: MarketDataEvent):
        entry = mid_price(event)
        return StrategySignal(
            symbol=event.symbol,
            direction=OrderSide.BUY,
            entry=entry,
            stop_loss=entry - 5,
            take_profit=entry + 10,
            confidence=0.9,
            strategy_name=self.name,
            timestamp=event.timestamp,
        )

    def calculate_entry(self, event, direction):
        return mid_price(event)

    def calculate_stop_loss(self, entry, direction):
        return None

    def calculate_take_profit(self, entry, direction):
        return None

    def confidence(self, event):
        return 0.9


class FakeFeed:
    def __init__(self, bars: list[Bar], quote: Quote | None) -> None:
        self.bars, self._quote = bars, quote

    async def latest_closed_bar(self, symbol):
        return self.bars[-1] if self.bars else None

    async def history(self, symbol, count):
        return self.bars[-count:]

    async def quote(self, symbol):
        return self._quote

    def add_bar(self) -> None:
        last = self.bars[-1]
        self.bars.append(Bar(last.time + timedelta(minutes=15), 2500, 2501, 2499, 2500, 10, 25))


def _bars(n: int = 3) -> list[Bar]:
    return [Bar(T0 + timedelta(minutes=15 * i), 2500, 2501, 2499, 2500, 10, 25) for i in range(n)]


async def _rig(balance: float = 10_000.0, **settings_kw):
    fake = FakeMT5(balance=balance)
    settings = Settings(execution_backend="mt5", **settings_kw)
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    feed = FakeFeed(_bars(), Quote(2500.0, 2500.3, 30.0, 0.0))
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=feed,
        signal_engine=SignalEngine([AlwaysBuy()]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=["XAUUSD"],
        poll_s=0.01,
        warmup_bars=3,
    )
    return loop, fake, feed, risk


async def test_warmup_never_trades_the_past() -> None:
    loop, fake, _, _ = await _rig()
    await loop.start()
    assert fake.sent == [] and loop._last_bar["XAUUSD"] == _bars()[-1].time


async def test_new_closed_bar_produces_one_correctly_sized_protected_order() -> None:
    loop, fake, feed, _ = await _rig()
    await loop.start()
    await loop.run_once()  # same bar as warm-up: nothing new
    assert fake.sent == []
    feed.add_bar()
    await loop.run_once()
    assert len(fake.sent) == 1
    order = fake.sent[0]
    assert order["sl"] and order["tp"] and order["magic"] == 26_092_101
    # $5 stop x $100/unit = $500/lot; 1% of $10k = $100 -> 0.20 lot, capped by max_position_size
    assert order["volume"] == pytest.approx(0.10)
    await loop.run_once()  # same bar again: nothing new
    assert len(fake.sent) == 1 and loop.stats.orders_sent == 1


async def test_second_signal_on_same_side_is_rejected_by_risk() -> None:
    loop, fake, feed, _ = await _rig()
    await loop.start()
    feed.add_bar()
    await loop.run_once()
    feed.add_bar()
    await loop.run_once()
    assert len(fake.sent) == 1
    assert loop.stats.skipped["risk:duplicate_position"] == 1


async def test_small_account_takes_no_trade() -> None:
    loop, fake, feed, _ = await _rig(balance=15.0)
    await loop.start()
    feed.add_bar()
    await loop.run_once()
    assert fake.sent == [] and loop.stats.skipped["size_too_small"] == 1


async def test_loop_passes_execution_entry_to_risk_from_current_quote() -> None:
    """The live hot path must use the current quote (ask/bid) for risk money math,
    not the stale signal.entry. Regression guard for the three-price mismatch."""
    loop, fake, feed, risk = await _rig()
    # Quote that differs from the bar close (which AlwaysBuy uses as signal.entry).
    feed._quote = Quote(2505.0, 2505.3, 30.0, 0.0)
    await loop.start()
    feed.add_bar()
    captured: list[float | None] = []
    real = risk.evaluate

    def capture(signal, ctx):
        captured.append(ctx.execution_entry)
        return real(signal, ctx)

    risk.evaluate = capture  # type: ignore[method-assign]
    await loop.run_once()
    assert len(fake.sent) == 1
    assert captured and captured[0] == pytest.approx(2505.3)  # BUY -> quote.ask


async def test_no_quote_means_no_trade() -> None:
    loop, fake, feed, _ = await _rig()
    feed._quote = None
    await loop.start()
    feed.add_bar()
    await loop.run_once()
    assert fake.sent == [] and loop.stats.skipped["no_quote"] == 1


async def test_stale_market_data_blocks_orders() -> None:
    loop, fake, feed, _ = await _rig()
    feed._quote = Quote(2500.0, 2500.3, 30.0, 60_000.0)  # tick unchanged for 60s
    await loop.start()
    feed.add_bar()
    await loop.run_once()
    assert fake.sent == []
    assert loop.stats.skipped["risk:stale_data_protection"] == 1


async def test_wide_spread_blocks_orders() -> None:
    loop, fake, feed, _ = await _rig()
    feed._quote = Quote(2500.0, 2503.0, 600.0, 0.0)
    await loop.start()
    feed.add_bar()
    await loop.run_once()
    assert fake.sent == [] and loop.stats.skipped["risk:spread_too_wide"] == 1


async def test_kill_switch_blocks_everything() -> None:
    loop, fake, feed, risk = await _rig()
    await loop.start()
    risk.engage_kill_switch("manual")
    feed.add_bar()
    await loop.run_once()
    assert fake.sent == []


async def test_daily_loss_breach_engages_kill_switch_via_monitor() -> None:
    loop, fake, feed, risk = await _rig()
    await loop.start()
    fake.profit = -1_000.0  # 10% floating loss: beyond the 3% daily cap
    feed.add_bar()
    await loop.run_once()
    assert risk.kill_switch and fake.sent == []


async def test_repeated_errors_halt_the_loop_and_engage_kill_switch() -> None:
    loop, fake, feed, risk = await _rig()
    loop.max_consecutive_errors = 3
    await loop.start()

    async def boom():
        raise RuntimeError("terminal gone")

    loop.adapter.get_account = boom  # type: ignore[method-assign]
    await asyncio.wait_for(loop.run(asyncio.Event()), timeout=5)
    assert risk.kill_switch and loop.stats.consecutive_errors == 3


async def test_loop_stops_cleanly_on_request() -> None:
    loop, _, _, _ = await _rig()
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert loop.stats.cycles >= 1 and loop.stats.errors == 0


# ------------------------------------------------------------------------------ MT5 feed


async def test_feed_returns_only_closed_bars_and_tracks_staleness() -> None:
    fake = FakeMT5()
    fake.rates = [
        {
            "time": 1704189600 + 900 * i,
            "open": 2000.0,
            "high": 2001.0,
            "low": 1999.0,
            "close": 2000.5,
            "tick_volume": 5,
            "spread": 25,
        }
        for i in range(5)
    ]
    adapter = MT5ExecutionAdapter(Settings(execution_backend="mt5"), client=fake)
    await adapter.connect()
    now = {"t": 100.0}
    feed = MT5CandleFeed(adapter, "M15", clock=lambda: now["t"])

    hist = await feed.history("XAUUSD", 3)
    assert len(hist) == 3 and hist[0].spread == 25
    last = await feed.latest_closed_bar("XAUUSD")
    assert last is not None and last.time == datetime.fromtimestamp(1704189600 + 900 * 3, UTC)

    q1 = await feed.quote("XAUUSD")
    assert q1.age_ms == 0 and q1.spread_points == pytest.approx(30.0)
    now["t"] = 107.0
    assert (await feed.quote("XAUUSD")).age_ms == pytest.approx(7000.0)  # tick unchanged
    fake.tick.time_msc = 2000  # market moved
    assert (await feed.quote("XAUUSD")).age_ms == 0
    fake.symbol = None  # unknown symbol data disappears
    assert await feed.quote("XAUUSD") is None
