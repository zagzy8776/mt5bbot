"""Free historical M1 candles from Dukascopy's public datafeed (no MT5/Windows needed).

Hardened for a flaky source: on-disk cache (resume after any failure), retries with
exponential backoff + jitter, 404/empty = market closed (not an error), per-day parse
failures are counted, and the binary layout is *verified* rather than trusted:

* column order is auto-detected (the layout whose OHLC is self-consistent wins),
* the price divisor is auto-detected against an expected price band for the symbol,
* a run where too many days fail raises instead of returning a partial, misleading file.

NOTE: the download path has not been exercised against the live server from this dev
sandbox (no external network); the parser is unit-tested on synthetic files that follow
the documented layout. Run `fetch-dukascopy --days 3` first and eyeball the output.
Use Dukascopy data within their terms of use; for an exact match to your broker's prices,
prefer `fetch-mt5`.
"""

from __future__ import annotations

import itertools
import lzma
import random
import statistics
import struct
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from mt5_platform.backtest.data import Bar, LoadReport, _clean_bars, resample, timeframe_minutes

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_RECORD = struct.Struct(">IIIIIf")  # secs-from-midnight, 4 price ints, volume
_ORDERS = list(itertools.permutations(range(4)))  # which of the 4 ints is O/H/L/C
# Record ints are documented as (open, close, low, high). An "order" is the tuple of indices
# (open_idx, high_idx, low_idx, close_idx) into those four ints.
_PREFERRED_ORDER = (0, 3, 2, 1)
_DIVISORS = (1, 10, 100, 1_000, 10_000, 100_000)
# Documented price scales; verified against PRICE_BANDS rather than trusted blindly.
KNOWN_DIVISORS = {"XAUUSD": 1_000, "EURUSD": 100_000, "GBPUSD": 100_000, "USDJPY": 1_000}

PRICE_BANDS: dict[str, tuple[float, float]] = {
    "XAUUSD": (500.0, 15_000.0),
    "EURUSD": (0.5, 2.0),
    "GBPUSD": (0.5, 2.5),
    "USDJPY": (50.0, 250.0),
    "BTCUSD": (1_000.0, 500_000.0),
}


def day_url(symbol: str, day: date) -> str:
    # Dukascopy months are ZERO-based in the URL.
    sym, y, m, d = symbol.upper(), day.year, day.month - 1, day.day
    return f"{BASE_URL}/{sym}/{y:04d}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"


def _consistent(o: float, h: float, low: float, c: float) -> bool:
    return h >= max(o, c) and low <= min(o, c) and h >= low


def parse_day(raw: bytes, day: date, symbol: str) -> list[Bar]:
    """Decode one day of M1 bid candles. Raises ValueError if the layout cannot be verified."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    if not data:
        return []
    if len(data) % _RECORD.size:
        raise ValueError(f"unexpected candle file size {len(data)} (record={_RECORD.size})")
    rows = list(_RECORD.iter_unpack(data))

    def score(order: tuple[int, int, int, int]) -> float:
        ok = sum(
            _consistent(r[1 + order[0]], r[1 + order[1]], r[1 + order[2]], r[1 + order[3]])
            for r in rows
        )
        return ok / len(rows)

    # Preferred layout first: max() keeps the first of equal scores (ties -> documented layout).
    candidates = [(_PREFERRED_ORDER, score(_PREFERRED_ORDER))] + [
        (o, score(o)) for o in _ORDERS if o != _PREFERRED_ORDER
    ]
    order, best = max(candidates, key=lambda x: x[1])
    if best < 0.99:
        raise ValueError(f"could not verify OHLC layout (best consistency {best:.0%})")

    lo, hi = PRICE_BANDS.get(symbol.upper(), (0.0, float("inf")))
    closes = [r[1 + order[3]] for r in rows]
    med = statistics.median(closes)
    fits = [d for d in _DIVISORS if lo <= med / d <= hi]
    known = KNOWN_DIVISORS.get(symbol.upper())
    if known in fits:
        divisor = known
    elif len(fits) == 1:
        divisor = fits[0]
    else:
        raise ValueError(
            f"cannot determine the price divisor: median {med} fits {fits or 'none'} "
            f"for expected band {lo}-{hi}"
        )

    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    bars = [
        Bar(
            start + timedelta(seconds=r[0]),
            r[1 + order[0]] / divisor,
            r[1 + order[1]] / divisor,
            r[1 + order[2]] / divisor,
            r[1 + order[3]] / divisor,
            float(r[5]),
        )
        for r in rows
    ]
    return bars


def _http_fetch(url: str, timeout: float = 30.0) -> bytes | None:
    import httpx

    resp = httpx.get(url, timeout=timeout, headers={"User-Agent": "mt5bbot-research/1.0"})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def download_range(
    symbol: str,
    start: date,
    end: date,
    *,
    timeframe: str = "M15",
    cache_dir: str | Path = "data/cache/dukascopy",
    fetch: Callable[[str], bytes | None] = _http_fetch,
    retries: int = 4,
    pause_s: float = 0.15,
    max_failed_fraction: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Bar], dict]:
    """Download [start, end] inclusive, resample to ``timeframe``. Returns (bars, stats)."""
    minutes = timeframe_minutes(timeframe)
    cache = Path(cache_dir) / symbol.upper()
    cache.mkdir(parents=True, exist_ok=True)
    stats = {"days": 0, "cached": 0, "downloaded": 0, "closed": 0, "failed": 0, "failed_days": []}
    m1: list[Bar] = []
    day = start
    while day <= end:
        stats["days"] += 1
        raw_path, empty_path = cache / f"{day}.bi5", cache / f"{day}.empty"
        raw: bytes | None
        if empty_path.exists():
            stats["cached"] += 1
            raw = b""
        elif raw_path.exists():
            stats["cached"] += 1
            raw = raw_path.read_bytes()
        else:
            raw = None
            if day.weekday() == 5:  # Saturdays: market closed, skip the request
                raw = b""
            else:
                for attempt in range(retries + 1):
                    try:
                        raw = fetch(day_url(symbol, day)) or b""
                        break
                    except Exception:  # network / 5xx: back off and retry
                        if attempt == retries:
                            raw = None
                        else:
                            sleep(min(30.0, 0.5 * 2**attempt) + random.random() * 0.25)
                if raw is not None:
                    stats["downloaded"] += 1
                    if raw:
                        raw_path.write_bytes(raw)
                    else:
                        empty_path.touch()
                    sleep(pause_s)
        if raw is None:
            stats["failed"] += 1
            stats["failed_days"].append(str(day))
        elif not raw:
            stats["closed"] += 1
        else:
            try:
                m1.extend(parse_day(raw, day, symbol))
            except (ValueError, lzma.LZMAError):
                stats["failed"] += 1
                stats["failed_days"].append(str(day))
                raw_path.unlink(missing_ok=True)  # do not cache a file we cannot read
        day += timedelta(days=1)

    open_days = stats["days"] - stats["closed"]
    if open_days and stats["failed"] / max(open_days, 1) > max_failed_fraction:
        raise RuntimeError(
            f"{stats['failed']} of {open_days} days failed ({stats['failed_days'][:5]}...): "
            "refusing to return a partial dataset. Check network/URL/layout; retry (cache resumes)."
        )
    report = LoadReport(bars=[], rows_read=len(m1))
    bars = _clean_bars(m1, report)
    stats["dropped"] = dict(report.dropped)
    stats["warnings"] = report.warnings
    return (resample(bars, minutes) if minutes > 1 else bars), stats
