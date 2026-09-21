"""Out-of-sample splitting and parameter sweeps that report how badly they overfit."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from mt5_platform.backtest.data import Bar
from mt5_platform.backtest.engine import BacktestConfig, run_backtest
from mt5_platform.backtest.metrics import Metrics, compute_metrics
from mt5_platform.strategy.registry import create_strategy


def split_bars(bars: list[Bar], oos_fraction: float = 0.3) -> tuple[list[Bar], list[Bar]]:
    """Chronological split. The OOS tail must never influence parameter choice."""
    if not 0.05 <= oos_fraction <= 0.6:
        raise ValueError("oos_fraction must be between 0.05 and 0.6")
    cut = int(len(bars) * (1 - oos_fraction))
    return bars[:cut], bars[cut:]


@dataclass
class SweepRow:
    params: dict
    in_sample: Metrics
    out_of_sample: Metrics | None
    score: float

    @property
    def degradation(self) -> float | None:
        """OOS profit factor / IS profit factor. Far below 1.0 = overfit."""
        if self.out_of_sample is None:
            return None
        a, b = self.in_sample.profit_factor, self.out_of_sample.profit_factor
        if math.isinf(a) or a <= 0:
            return None
        return min(b, 10.0) / a


@dataclass
class SweepReport:
    rows: list[SweepRow]
    combos_tested: int
    combos_invalid: int

    @property
    def multiple_testing_warning(self) -> str | None:
        if self.combos_tested >= 10:
            return (
                f"{self.combos_tested} parameter sets were tried on the same data: the best "
                "in-sample result is partly luck. Trust only the out-of-sample columns."
            )
        return None


def sweep(
    strategy: str,
    grid: dict[str, list],
    bars: list[Bar],
    config: BacktestConfig,
    *,
    oos_fraction: float = 0.3,
    top_k: int = 5,
    min_trades: int = 30,
) -> SweepReport:
    train, test = split_bars(bars, oos_fraction)
    keys = list(grid)
    scored: list[SweepRow] = []
    invalid = tested = 0
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values, strict=True))
        try:
            strat = create_strategy(strategy, **params)
        except (ValueError, TypeError):
            invalid += 1
            continue
        tested += 1
        m = compute_metrics(run_backtest(train, [strat], config))
        score = (
            m.net_profit / max(m.max_drawdown_pct, 1.0) if m.n_trades >= min_trades else -math.inf
        )
        scored.append(SweepRow(params, m, None, score))
    scored.sort(key=lambda r: r.score, reverse=True)
    top = scored[:top_k]
    for row in top:
        strat = create_strategy(strategy, **row.params)
        row.out_of_sample = compute_metrics(run_backtest(test, [strat], config))
    return SweepReport(rows=top, combos_tested=tested, combos_invalid=invalid)
