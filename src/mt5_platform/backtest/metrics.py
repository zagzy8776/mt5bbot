"""Backtest metrics and the go/no-go gates a strategy must clear before real money."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from mt5_platform.backtest.engine import BacktestResult


@dataclass
class Metrics:
    starting_balance: float
    final_equity: float
    net_profit: float
    return_pct: float
    n_trades: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    expectancy_money: float
    expectancy_r: float
    max_drawdown_pct: float
    longest_losing_streak: int
    weeks: int
    avg_weekly_pnl: float
    pct_weeks_profitable: float
    weekly_pnl: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["profit_factor"] = None if math.isinf(self.profit_factor) else self.profit_factor
        return d

    def weeks_at_or_above(self, target: float) -> float:
        """Fraction of weeks whose P/L reached ``target`` (e.g. your $100/week goal)."""
        return (
            sum(1 for w in self.weekly_pnl if w >= target) / len(self.weekly_pnl)
            if self.weekly_pnl
            else 0.0
        )


def compute_metrics(result: BacktestResult) -> Metrics:
    start = result.config.starting_balance
    trades = result.trades
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)

    peak, max_dd = start, 0.0
    for _, eq in result.equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)

    streak = longest = 0
    for t in trades:
        streak = streak + 1 if t.pnl <= 0 else 0
        longest = max(longest, streak)

    weekly: list[float] = []
    prev = start
    last_key, last_eq = None, start
    for when, eq in result.equity_curve:
        iso = when.isocalendar()
        key = (iso.year, iso.week)
        if last_key is not None and key != last_key:
            weekly.append(last_eq - prev)
            prev = last_eq
        last_key, last_eq = key, eq
    if last_key is not None:
        weekly.append(last_eq - prev)

    n = len(trades)
    final = result.final_equity
    return Metrics(
        starting_balance=start,
        final_equity=final,
        net_profit=final - start,
        return_pct=(final - start) / start * 100.0 if start else 0.0,
        n_trades=n,
        win_rate=len(wins) / n if n else 0.0,
        profit_factor=pf,
        avg_win=gross_win / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        expectancy_money=sum(t.pnl for t in trades) / n if n else 0.0,
        expectancy_r=sum(t.r_multiple for t in trades) / n if n else 0.0,
        max_drawdown_pct=max_dd,
        longest_losing_streak=longest,
        weeks=len(weekly),
        avg_weekly_pnl=sum(weekly) / len(weekly) if weekly else 0.0,
        pct_weeks_profitable=sum(1 for w in weekly if w > 0) / len(weekly) if weekly else 0.0,
        weekly_pnl=weekly,
    )


@dataclass
class GateReport:
    passed: bool
    checks: list[tuple[str, bool, str]]

    def lines(self) -> list[str]:
        return [
            f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}" for name, ok, detail in self.checks
        ]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
        }


def evaluate_gates(
    in_sample: Metrics,
    out_of_sample: Metrics | None,
    *,
    min_trades: int = 100,
    min_profit_factor: float = 1.3,
    max_drawdown_pct: float = 20.0,
    oos_min_trades: int = 30,
    oos_min_profit_factor: float = 1.1,
) -> GateReport:
    """Necessary-not-sufficient checks. Passing means "not obviously broken", not "profitable"."""
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    add(
        "enough trades",
        in_sample.n_trades >= min_trades,
        f"{in_sample.n_trades} (need {min_trades})",
    )
    add(
        "profit factor",
        in_sample.profit_factor >= min_profit_factor,
        f"{in_sample.profit_factor:.2f} (need {min_profit_factor})",
    )
    add(
        "expectancy > 0",
        in_sample.expectancy_money > 0,
        f"{in_sample.expectancy_money:.4f} per trade",
    )
    add(
        "drawdown",
        in_sample.max_drawdown_pct <= max_drawdown_pct,
        f"{in_sample.max_drawdown_pct:.1f}% (max {max_drawdown_pct}%)",
    )
    if out_of_sample is None:
        add("out-of-sample test", False, "not run — in-sample results alone prove nothing")
    else:
        add(
            "OOS enough trades",
            out_of_sample.n_trades >= oos_min_trades,
            f"{out_of_sample.n_trades} (need {oos_min_trades})",
        )
        add(
            "OOS profit factor",
            out_of_sample.profit_factor >= oos_min_profit_factor,
            f"{out_of_sample.profit_factor:.2f} (need {oos_min_profit_factor})",
        )
        add("OOS profitable", out_of_sample.net_profit > 0, f"{out_of_sample.net_profit:+.2f}")
        add(
            "OOS drawdown",
            out_of_sample.max_drawdown_pct <= max_drawdown_pct,
            f"{out_of_sample.max_drawdown_pct:.1f}%",
        )
    return GateReport(passed=all(ok for _, ok, _ in checks), checks=checks)
