"""Backtest data hardening, engine correctness (no lookahead, costs, SL-first), metrics, gates."""

from __future__ import annotations

import dataclasses
import json
import lzma
import struct
from datetime import UTC, date, datetime, timedelta

import pytest

from mt5_platform.backtest import (
    BacktestConfig,
    BacktestResult,
    Bar,
    Trade,
    compute_metrics,
    evaluate_gates,
    load_csv,
    resample,
    run_backtest,
    split_bars,
    sweep,
    synthetic_bars,
)
from mt5_platform.backtest.__main__ import main as backtest_cli
from mt5_platform.backtest.data import fetch_mt5_bars, parse_time, write_csv
from mt5_platform.backtest.dukascopy import day_url, download_range, parse_day
from mt5_platform.backtest.validation import live_block_reason, read_validation, write_validation
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from tests.fake_mt5 import FakeMT5

T0 = datetime(2024, 1, 2, tzinfo=UTC)  # a Tuesday


def bar(i: int, o: float, h: float, low: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(T0 + timedelta(minutes=15 * i), o, h, low, c, 100.0, spread)


class Scripted(Strategy):
    """Emits pre-planned signals keyed by the bar-close time it sees."""

    name = "scripted"

    def __init__(self, plan: dict[datetime, tuple[OrderSide, float, float | None]]) -> None:
        super().__init__()
        self.plan = plan

    def generate_signal(self, event: MarketDataEvent):
        step = self.plan.get(event.timestamp)
        if step is None:
            return None
        side, sl, tp = step
        return StrategySignal(
            symbol=event.symbol,
            direction=side,
            entry=mid_price(event),
            stop_loss=sl,
            take_profit=tp,
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


CFG = BacktestConfig(
    starting_balance=10_000.0,
    risk_pct=1.0,
    max_volume=1.0,
    max_exposure_pct=100_000.0,
    default_spread_price=0.30,
    slippage_price=0.05,
)


def _run(bars, plan, cfg=CFG):
    return run_backtest(bars, [Scripted({bars[k].time: v for k, v in plan.items()})], cfg)


# ------------------------------------------------------------------------- data loading


def test_loads_mt5_export_format(tmp_path) -> None:
    f = tmp_path / "x.csv"
    f.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2024.01.02\t10:00:00\t2000.5\t2001.0\t1999.5\t2000.8\t120\t0\t25\n"
        "2024.01.02\t10:15:00\t2000.8\t2002.0\t2000.0\t2001.5\t99\t0\t30\n",
        encoding="utf-8",
    )
    rep = load_csv(f)
    assert len(rep.bars) == 2 and rep.bars[0].time == datetime(2024, 1, 2, 10, tzinfo=UTC)
    assert rep.bars[0].spread == 25 and rep.bars[1].volume == 99


def test_bad_rows_are_dropped_and_counted_not_kept(tmp_path) -> None:
    f = tmp_path / "x.csv"
    f.write_text(
        "time,open,high,low,close\n"
        "2024-01-02 10:15:00,10,11,9,10.5\n"  # out of order (sorted)
        "2024-01-02 10:00:00,10,11,9,10\n"
        "2024-01-02 10:00:00,10,11,9,10\n"  # duplicate time
        "2024-01-02 10:30:00,10,9,11,10\n"  # high < low
        "2024-01-02 10:45:00,nan,11,9,10\n"  # NaN
        "2024-01-02 11:00:00,abc,11,9,10\n"  # junk
        "2024-01-02 11:15:00,-1,11,-2,10\n",  # negative
        encoding="utf-8",
    )
    rep = load_csv(f)
    assert [b.time.minute for b in rep.bars] == [0, 15]
    assert rep.dropped["duplicate_time"] == 1
    assert rep.dropped["bad_ohlc"] + rep.dropped["non_positive_or_nan"] == 3
    assert rep.dropped["unparseable"] == 1
    assert any("out of time order" in w for w in rep.warnings)


def test_missing_columns_fail_loudly(tmp_path) -> None:
    f = tmp_path / "x.csv"
    f.write_text("time,open,close\n2024-01-01,1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="open/high/low/close"):
        load_csv(f)


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-02 10:00",
        "2024.01.02 10:00:00",
        "2024-01-02T10:00:00Z",
        "1704189600",
        "1704189600000",
    ],
)
def test_parse_time_formats(text) -> None:
    assert parse_time(text) == datetime(2024, 1, 2, 10, tzinfo=UTC)


def test_csv_roundtrip_and_resample(tmp_path) -> None:
    bars = [bar(i, 100 + i, 101 + i, 99 + i, 100.5 + i, 20) for i in range(8)]
    write_csv(bars, tmp_path / "r.csv")
    again = load_csv(tmp_path / "r.csv").bars
    assert len(again) == 8 and again[3].close == bars[3].close
    h1 = resample(again, 60)
    assert len(h1) == 2
    assert h1[0].open == 100 and h1[0].close == 103.5 and h1[0].high == 104 and h1[0].low == 99
    assert h1[0].volume == 400


def test_mt5_fetch_uses_broker_history() -> None:
    fake = FakeMT5()
    fake.rates = [
        {
            "time": 1704189600 + 900 * i,
            "open": 2000.0,
            "high": 2001.0,
            "low": 1999.0,
            "close": 2000.5,
            "tick_volume": 10,
            "spread": 25,
        }
        for i in range(3)
    ]
    bars = fetch_mt5_bars(fake, "XAUUSD", "M15", T0, T0 + timedelta(days=1))
    assert len(bars) == 3 and bars[0].spread == 25
    fake.rates = []
    with pytest.raises(RuntimeError, match="no history"):
        fetch_mt5_bars(fake, "XAUUSD", "M15", T0, T0)


# ---------------------------------------------------------------------- engine honesty


def test_signal_fills_at_next_bar_open_with_spread_and_slippage() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 2009, 2011),
        bar(2, 2011, 2031, 2010, 2030),
    ]
    res = _run(bars, {0: (OrderSide.BUY, 1990.0, 2030.0)})
    (t,) = res.trades
    assert t.opened_at == bars[1].time  # NOT bar 0: no lookahead
    assert t.entry == pytest.approx(2010 + 0.30 + 0.05)  # ask + adverse slippage
    assert t.exit == 2030.0 and t.exit_reason == "tp"
    assert t.volume == pytest.approx(0.04)  # $100 risk / ($20.35 x $100/unit)
    assert t.pnl == pytest.approx((2030 - 2010.35) * 100 * 0.04)


def test_stop_wins_when_stop_and_target_share_a_bar() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 2009, 2011),
        bar(2, 2010, 2035, 1985, 2000),
    ]
    (t,) = _run(bars, {0: (OrderSide.BUY, 1990.0, 2030.0)}).trades
    assert t.exit_reason == "sl" and t.exit == pytest.approx(1990 - 0.05) and t.pnl < 0


def test_gap_through_stop_fills_at_the_gap_not_the_stop() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 2009, 2011),
        bar(2, 1980, 1985, 1975, 1982),
    ]
    (t,) = _run(bars, {0: (OrderSide.BUY, 1990.0, 2030.0)}).trades
    assert t.exit == pytest.approx(1980 - 0.05)
    assert t.r_multiple < -1.0  # worse than the planned 1R loss


def test_short_enters_at_bid_and_exits_at_ask() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 2009, 2011),
        bar(2, 2010, 2011, 1989, 1995),
    ]
    (t,) = _run(bars, {0: (OrderSide.SELL, 2030.0, 1990.0)}).trades
    assert t.entry == pytest.approx(2010 - 0.05)
    assert t.exit_reason == "tp" and t.exit == 1990.0  # low+spread (1989.3) <= tp
    assert t.pnl > 0


def test_costs_alone_lose_money_on_a_flat_market() -> None:
    flat = [bar(i, 2000, 2000, 2000, 2000) for i in range(4)]
    res = _run(flat, {0: (OrderSide.BUY, 1990.0, 2010.0)})
    (t,) = res.trades
    assert t.exit_reason == "end"
    assert t.pnl == pytest.approx(-(0.30 + 0.05) * 100 * t.volume)


def test_small_account_takes_no_trade_instead_of_rounding_up() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 2009, 2011),
        bar(2, 2011, 2031, 2010, 2030),
    ]
    res = _run(
        bars, {0: (OrderSide.BUY, 1990.0, 2030.0)}, dataclasses.replace(CFG, starting_balance=15.0)
    )
    assert res.trades == [] and res.skips["size_too_small"] == 1
    assert res.final_equity == 15.0


def test_one_position_per_direction_and_position_cap() -> None:
    bars = [bar(i, 2010, 2011, 2009, 2010) for i in range(5)]
    res = _run(bars, {0: (OrderSide.BUY, 1990.0, 2100.0), 1: (OrderSide.BUY, 1990.0, 2100.0)})
    assert res.skips["duplicate_direction"] == 1 and len(res.trades) == 1  # closed at end


def test_drawdown_halt_stops_new_trades() -> None:
    bars = [
        bar(0, 2000, 2001, 1999, 2000),
        bar(1, 2010, 2012, 1985, 1985),
        bar(2, 1985, 1990, 1980, 1985),
        bar(3, 1985, 1990, 1980, 1985),
    ]
    cfg = dataclasses.replace(CFG, max_drawdown_pct=0.5)
    res = _run(bars, {0: (OrderSide.BUY, 1990.0, 2030.0), 2: (OrderSide.BUY, 1970.0, 2030.0)}, cfg)
    assert res.halted and res.skips["halted"] == 1


def test_backtests_are_deterministic() -> None:
    bars = synthetic_bars(3000, seed=3)
    from mt5_platform.strategy.registry import create_strategy

    a = run_backtest(
        bars, [create_strategy("sma_crossover")], dataclasses.replace(CFG, max_exposure_pct=300)
    )
    b = run_backtest(
        bars, [create_strategy("sma_crossover")], dataclasses.replace(CFG, max_exposure_pct=300)
    )
    assert [t.pnl for t in a.trades] == [t.pnl for t in b.trades] and a.trades


# ----------------------------------------------------------------------------- metrics


def _result(pnls, curve) -> BacktestResult:
    trades = [
        Trade("s", OrderSide.BUY, 0.01, 1, 1, 0.5, T0, T0, p, p / 5, "tp" if p > 0 else "sl")
        for p in pnls
    ]
    return BacktestResult(
        BacktestConfig(starting_balance=100.0),
        trades,
        curve,
        __import__("collections").Counter(),
        10,
    )


def test_metrics_math() -> None:
    d = lambda day, eq: (datetime(2024, 1, day, 12, tzinfo=UTC), eq)  # noqa: E731
    res = _result([10, -5, 10, -5, -5], [d(2, 110), d(4, 105), d(9, 115), d(11, 110)])
    m = compute_metrics(res)
    assert m.n_trades == 5 and m.win_rate == pytest.approx(0.4)
    assert m.profit_factor == pytest.approx(20 / 15)
    assert m.longest_losing_streak == 2
    assert m.max_drawdown_pct == pytest.approx(5 / 110 * 100)
    assert (
        m.weekly_pnl == [5.0, 5.0]
        and m.weeks_at_or_above(5) == 1.0
        and m.weeks_at_or_above(6) == 0.0
    )
    assert m.net_profit == pytest.approx(10.0)


def test_gates_require_out_of_sample_evidence() -> None:
    good = compute_metrics(
        _result([10] * 60 + [-5] * 40, [(T0, 100.0), (T0 + timedelta(days=1), 400.0)])
    )
    assert evaluate_gates(good, good).passed
    assert not evaluate_gates(good, None).passed  # in-sample alone proves nothing
    weak_oos = dataclasses.replace(good, profit_factor=0.9, net_profit=-3.0)
    assert not evaluate_gates(good, weak_oos).passed
    few = dataclasses.replace(good, n_trades=20)
    assert not evaluate_gates(few, good).passed


def test_sweep_reports_oos_and_flags_multiple_testing() -> None:
    bars = synthetic_bars(6000, seed=11)
    cfg = dataclasses.replace(CFG, starting_balance=5000.0, max_exposure_pct=300)
    out = sweep(
        "sma_crossover",
        {"fast_period": [3, 5, 8, 12, 40], "slow_period": [20, 30, 50]},
        bars,
        cfg,
        top_k=3,
        min_trades=10,
    )
    assert out.combos_invalid == 2 and out.combos_tested == 13  # fast>=slow rejected
    assert len(out.rows) == 3 and all(r.out_of_sample is not None for r in out.rows)
    assert out.multiple_testing_warning
    train, test = split_bars(bars, 0.3)
    assert len(train) + len(test) == len(bars) and train[-1].time < test[0].time


# ---------------------------------------------------------------------- validation gate


def test_live_needs_a_passing_matching_report(tmp_path) -> None:
    path = tmp_path / "v.json"
    assert "no validation report" in live_block_reason(
        read_validation(path), symbol="XAUUSD", timeframe="M15"
    )
    m = compute_metrics(
        _result([10] * 60 + [-5] * 40, [(T0, 100.0), (T0 + timedelta(days=1), 400.0)])
    )
    good = evaluate_gates(m, m)
    write_validation(
        path,
        symbol="XAUUSD",
        timeframe="M15",
        strategy="sma_crossover",
        params={"fast_period": 5},
        gates=good,
        in_sample=m,
        out_of_sample=m,
        data_range=("2024-01-01", "2024-06-01"),
    )
    rep = read_validation(path)
    assert live_block_reason(rep, symbol="xauusd", timeframe="m15") is None
    assert "not EURUSD" in live_block_reason(rep, symbol="EURUSD", timeframe="M15")
    rep["passed"] = False
    assert "FAILED" in live_block_reason(rep, symbol="XAUUSD", timeframe="M15")
    path.write_text("{corrupt", encoding="utf-8")
    assert read_validation(path) is None


# --------------------------------------------------------------------------- dukascopy


def _day_file(
    order: str = "ocl h", *, scale: int = 1000, base: float = 2000.0, n: int = 5
) -> bytes:
    """Synthetic day in the documented layout: ints (open, close, low, high) + volume."""
    recs = []
    for i in range(n):
        o, c, low, h = (round((base + i + d) * scale) for d in (0.0, 0.5, -0.4, 0.9))
        ints = {"o": o, "c": c, "l": low, "h": h}
        recs.append(struct.pack(">IIIIIf", i * 60, *[ints[k] for k in ("o", "c", "l", "h")], 1.5))
    return lzma.compress(b"".join(recs))


def test_parse_day_reads_documented_layout() -> None:
    bars = parse_day(_day_file(), date(2024, 1, 2), "XAUUSD")
    assert len(bars) == 5
    assert bars[0].open == 2000.0 and bars[0].close == 2000.5
    assert bars[0].low == 1999.6 and bars[0].high == 2000.9
    assert bars[1].time == datetime(2024, 1, 2, 0, 1, tzinfo=UTC)


def test_parse_day_detects_a_different_column_order() -> None:
    recs = []
    for i in range(5):
        o, h, low, c = (round((2000 + i + d) * 1000) for d in (0.0, 0.9, -0.4, 0.5))
        recs.append(struct.pack(">IIIIIf", i * 60, o, h, low, c, 1.0))  # o,h,l,c order
    bars = parse_day(lzma.compress(b"".join(recs)), date(2024, 1, 2), "XAUUSD")
    assert bars[0].open == 2000.0 and bars[0].high == 2000.9 and bars[0].close == 2000.5


def test_parse_day_autodetects_price_divisor() -> None:
    bars = parse_day(_day_file(scale=100), date(2024, 1, 2), "XAUUSD")
    assert bars[0].open == pytest.approx(2000.0)


def test_parse_day_refuses_garbage_and_wrong_prices() -> None:
    with pytest.raises(ValueError):
        parse_day(lzma.compress(b"x" * 7), date(2024, 1, 2), "XAUUSD")
    junk = (
        struct.pack(">IIIIIf", 0, 5, 1, 9, 3, 1.0) * 30
        + struct.pack(">IIIIIf", 0, 9, 1, 5, 3, 1.0) * 30
    )
    with pytest.raises(ValueError, match="verify OHLC"):
        parse_day(lzma.compress(junk), date(2024, 1, 2), "XAUUSD")
    with pytest.raises(ValueError, match="price divisor"):
        parse_day(_day_file(base=5.0), date(2024, 1, 2), "XAUUSD")
    assert parse_day(b"", date(2024, 1, 2), "XAUUSD") == []


def test_day_url_uses_zero_based_months() -> None:
    assert day_url("xauusd", date(2024, 1, 2)).endswith("/XAUUSD/2024/00/02/BID_candles_min_1.bi5")


def test_download_caches_retries_and_skips_closed_days(tmp_path) -> None:
    calls: list[str] = []
    flaky = {"n": 0}

    def fetch(url: str):
        calls.append(url)
        if "/00/03/" in url:  # Wed 3 Jan (URL months are 0-based): fail twice then work
            flaky["n"] += 1
            if flaky["n"] <= 2:
                raise ConnectionError("boom")
        if "/00/07/" in url:  # Sunday: closed
            return None
        return _day_file()

    kw = dict(cache_dir=tmp_path, fetch=fetch, sleep=lambda s: None, timeframe="M1")
    bars, stats = download_range("XAUUSD", date(2024, 1, 2), date(2024, 1, 7), **kw)
    assert len(bars) == 4 * 5  # Tue, Wed, Thu, Fri; Sat skipped; Sun closed
    assert stats["closed"] == 2 and stats["failed"] == 0 and flaky["n"] == 3
    assert not any("/00/06/" in c for c in calls)  # Saturday never requested
    first_calls = len(calls)
    bars2, stats2 = download_range("XAUUSD", date(2024, 1, 2), date(2024, 1, 7), **kw)
    assert len(calls) == first_calls and stats2["cached"] == 5 and len(bars2) == len(bars)


def test_download_refuses_partial_datasets(tmp_path) -> None:
    def down(url: str):
        raise ConnectionError("down")

    with pytest.raises(RuntimeError, match="partial dataset"):
        download_range(
            "XAUUSD",
            date(2024, 1, 2),
            date(2024, 1, 5),
            cache_dir=tmp_path,
            fetch=down,
            sleep=lambda s: None,
            retries=1,
        )


# ------------------------------------------------------------------------------ the CLI


def test_cli_end_to_end_writes_verdict(tmp_path, capsys) -> None:
    csv_path, verdict = tmp_path / "s.csv", tmp_path / "v.json"
    assert backtest_cli(["synth", "--out", str(csv_path), "--n", "6000"]) == 0
    code = backtest_cli(
        ["run", "--csv", str(csv_path), "--balance", "5000", "--validation-out", str(verdict)]
    )
    out = capsys.readouterr().out
    assert code == 1 and "NOT READY for live money" in out  # a random walk has no edge
    assert json.loads(verdict.read_text())["passed"] is False


def test_cli_tells_a_tiny_account_why_it_cannot_trade(tmp_path, capsys) -> None:
    csv_path = tmp_path / "s.csv"
    backtest_cli(["synth", "--out", str(csv_path), "--n", "6000"])
    backtest_cli(
        [
            "run",
            "--csv",
            str(csv_path),
            "--balance",
            "15",
            "--validation-out",
            str(tmp_path / "v.json"),
        ]
    )
    out = capsys.readouterr().out
    assert "too small" in out and "size_too_small" in out
