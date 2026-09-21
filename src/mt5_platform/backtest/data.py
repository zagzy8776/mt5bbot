"""Historical bars: hardened loading, validation, resampling, and MT5 download.

Bars are BID-based (MT5 convention). Spread is applied by the engine, so results include
the cost of crossing the spread. Bad rows are dropped and *counted*, never silently kept.
"""

from __future__ import annotations

import csv
import math
import random
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mt5_platform.common.events import MarketDataEvent


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime  # bar OPEN time, UTC-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0  # points (MT5 convention); 0 = unknown -> engine default


@dataclass
class LoadReport:
    bars: list[Bar]
    rows_read: int = 0
    dropped: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        dropped = ", ".join(f"{k}={v}" for k, v in sorted(self.dropped.items())) or "none"
        lines = [f"{len(self.bars)} bars kept of {self.rows_read} rows (dropped: {dropped})"]
        lines += [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def bar_to_event(bar: Bar, symbol: str, spread_price: float) -> MarketDataEvent:
    """Bar close as a market event. Shared by backtest and live runtime so they agree."""
    return MarketDataEvent(
        timestamp=bar.time,
        source="bars",
        symbol=symbol,
        bid=bar.close,
        ask=bar.close + spread_price,
        price=bar.close + spread_price / 2.0,
        volume=bar.volume,
        spread=spread_price,
    )


# --------------------------------------------------------------------------- parsing

_DOTTED_DATE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})")


def parse_time(text: str) -> datetime:
    s = text.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):  # epoch seconds or milliseconds
        v = float(s)
        return datetime.fromtimestamp(v / 1000.0 if v > 1e11 else v, UTC)
    s = _DOTTED_DATE.sub(r"\1-\2-\3", s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("tickvol", "tick_volume", "volume", "vol"),
    "spread": ("spread",),
}


def _find(idx: dict[str, int], names: tuple[str, ...]) -> int | None:
    for n in names:
        if n in idx:
            return idx[n]
    return None


def _clean_bars(rows: list[Bar], report: LoadReport) -> list[Bar]:
    eps = 1e-9
    good: list[Bar] = []
    for b in rows:
        vals = (b.open, b.high, b.low, b.close)
        if not all(math.isfinite(v) and v > 0 for v in vals):
            report.dropped["non_positive_or_nan"] += 1
        elif b.high < max(b.open, b.close) - eps or b.low > min(b.open, b.close) + eps:
            report.dropped["bad_ohlc"] += 1
        else:
            good.append(b)
    if any(good[i].time > good[i + 1].time for i in range(len(good) - 1)):
        report.warnings.append("rows were out of time order; sorted")
        good.sort(key=lambda b: b.time)
    unique: list[Bar] = []
    for b in good:
        if unique and b.time == unique[-1].time:
            report.dropped["duplicate_time"] += 1
        else:
            unique.append(b)
    if len(unique) > 10:
        deltas = [
            (unique[i + 1].time - unique[i].time).total_seconds() for i in range(len(unique) - 1)
        ]
        step = statistics.median(deltas)
        big = sum(1 for d in deltas if d > step * 3)
        if step > 0 and big:
            report.warnings.append(
                f"{big} time gaps > 3x the {step / 60:.0f}min bar size "
                "(weekends/holidays are normal; "
                "many more means missing data)"
            )
    return unique


def load_csv(path: str | Path) -> LoadReport:
    """Load MT5-export or generic OHLC CSV (tab/comma/semicolon). Validates every row."""
    lines = [ln for ln in Path(path).read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path}: no data rows")
    delim = max("\t;,", key=lambda d: lines[0].count(d))
    if lines[0].count(delim) == 0:
        raise ValueError(f"{path}: cannot detect delimiter in header {lines[0]!r}")
    reader = csv.reader(lines, delimiter=delim)
    header = [h.strip().strip("<>").lower() for h in next(reader)]
    idx = {h: i for i, h in enumerate(header)}
    cols = {k: _find(idx, v) for k, v in _ALIASES.items()}
    if any(cols[k] is None for k in ("open", "high", "low", "close")):
        raise ValueError(f"{path}: need open/high/low/close columns, found {header}")
    report = LoadReport(bars=[])
    parsed: list[Bar] = []
    for row in reader:
        report.rows_read += 1
        try:
            if (
                "date" in idx
                and "time" in idx
                and ":" in row[idx["time"]]
                and not re.search(r"\d{4}", row[idx["time"]])
            ):
                ts = parse_time(f"{row[idx['date']].strip()} {row[idx['time']].strip()}")
            else:
                key = next(k for k in ("datetime", "time", "timestamp", "date") if k in idx)
                ts = parse_time(row[idx[key]])
            vol = float(row[cols["volume"]]) if cols["volume"] is not None else 0.0
            spr = float(row[cols["spread"]]) if cols["spread"] is not None else 0.0
            parsed.append(
                Bar(
                    ts,
                    float(row[cols["open"]]),
                    float(row[cols["high"]]),
                    float(row[cols["low"]]),
                    float(row[cols["close"]]),
                    vol,
                    max(spr, 0.0) if math.isfinite(spr) else 0.0,
                )
            )
        except (ValueError, IndexError, StopIteration):
            report.dropped["unparseable"] += 1
    report.bars = _clean_bars(parsed, report)
    return report


def write_csv(bars: list[Bar], path: str | Path) -> None:
    out = ["time,open,high,low,close,volume,spread"]
    for b in bars:
        out.append(
            f"{b.time:%Y-%m-%d %H:%M:%S},{b.open},{b.high},{b.low},{b.close},{b.volume},{b.spread}"
        )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """Aggregate sorted bars into ``minutes``-sized buckets (open-time aligned)."""
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    out: list[Bar] = []
    bucket: list[Bar] = []
    key = None
    step = minutes * 60
    for b in bars:
        k = int(b.time.timestamp()) // step
        if key is not None and k != key:
            out.append(_merge(bucket, key * step))
            bucket = []
        key = k
        bucket.append(b)
    if bucket and key is not None:
        out.append(_merge(bucket, key * step))
    return out


def _merge(bucket: list[Bar], start_epoch: int) -> Bar:
    spreads = [b.spread for b in bucket if b.spread > 0]
    return Bar(
        time=datetime.fromtimestamp(start_epoch, UTC),
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
        spread=(sum(spreads) / len(spreads)) if spreads else 0.0,
    )


# ------------------------------------------------------------------------ MT5 download

_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def timeframe_minutes(tf: str) -> int:
    try:
        return _TF_MINUTES[tf.upper()]
    except KeyError:
        raise ValueError(f"unknown timeframe {tf!r}; use one of {sorted(_TF_MINUTES)}") from None


def fetch_mt5_bars(
    client: Any, symbol: str, timeframe: str, date_from: datetime, date_to: datetime
) -> list[Bar]:
    """Download broker history through the MT5 terminal (Windows). Best data for backtests:
    it is the exact price feed your broker will execute against. Times are broker-server
    time expressed as epoch (not necessarily UTC) — consistent within one broker."""
    timeframe_minutes(timeframe)
    if not client.initialize():
        raise ConnectionError(f"MT5 initialize failed: {client.last_error()}")
    client.symbol_select(symbol, True)
    rates = client.copy_rates_range(
        symbol, getattr(client, f"TIMEFRAME_{timeframe.upper()}"), date_from, date_to
    )
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"no history returned for {symbol} {timeframe}: {client.last_error()}")
    bars = []
    for r in rates:
        bars.append(
            Bar(
                datetime.fromtimestamp(int(r["time"]), UTC),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["tick_volume"]),
                float(r["spread"]),
            )
        )
    report = LoadReport(bars=[], rows_read=len(bars))
    cleaned = _clean_bars(bars, report)
    if report.dropped or report.warnings:
        print(report.summary())
    return cleaned


# --------------------------------------------------------------------------- synthetic


def synthetic_bars(
    n: int = 5000,
    *,
    start_price: float = 2000.0,
    minutes: int = 15,
    seed: int = 7,
    vol: float = 0.0009,
    drift: float = 0.0,
    spread_points: float = 25.0,
) -> list[Bar]:
    """Seeded random-walk gold-like bars (weekends skipped). For tests/smoke runs only —
    a random walk has no edge, which makes it a useful null hypothesis for strategies."""
    rng = random.Random(seed)
    t = datetime(2024, 1, 1, tzinfo=UTC)
    price = start_price
    sigma = vol
    bars: list[Bar] = []
    while len(bars) < n:
        if t.weekday() >= 5:
            t += timedelta(minutes=minutes)
            continue
        sigma = 0.9 * sigma + 0.1 * vol * (0.5 + rng.random()) + 0.02 * vol * abs(rng.gauss(0, 1))
        o = price
        path = [o]
        for _ in range(6):
            path.append(path[-1] * (1 + drift + sigma * rng.gauss(0, 1) / 2.4))
        c = path[-1]
        bars.append(Bar(t, o, max(path), min(path), c, float(rng.randint(50, 500)), spread_points))
        price = c
        t += timedelta(minutes=minutes)
    return bars
