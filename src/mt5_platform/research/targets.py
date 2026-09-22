"""Turning a dollar target into the three things that decide it: edge, frequency, size.

P&L is multiplicative, not aspirational:

    expected_daily = trades_per_day x risk_per_trade_$ x (edge_R - cost_R)

so a target can only be met by a combination of a real net edge, enough independent trades, and
enough size. This module solves that equation for gold and reports what each answer *risks*, because
the same $20/day is trivial on a large account and ruinous on a small one.

Nothing here lowers a gate or promises a return: it states what is required, and what it costs to
find out (including how many trades are needed before an edge could be told apart from noise).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

# XAUUSD contract: 100 oz per lot, so a $1 price move is $100 per lot.
GOLD_CONTRACT_SIZE = 100.0
# Sigma_R measured, not assumed: the 8.1 report's observed p-values imply ~1.34 for gold M15.
DEFAULT_SIGMA_R = 1.34


def stop_distance_price(price: float, stop_pct: float) -> float:
    """Stop distance in price units from a percentage of price."""
    if price <= 0 or stop_pct <= 0:
        raise ValueError("price and stop_pct must be > 0")
    return price * stop_pct / 100.0


def risk_per_trade(price: float, stop_pct: float, lots: float) -> float:
    """Money at risk on one trade, in account currency."""
    if lots < 0:
        raise ValueError("lots must be >= 0")
    return stop_distance_price(price, stop_pct) * GOLD_CONTRACT_SIZE * lots


def lots_for_risk(price: float, stop_pct: float, risk_amount: float) -> float:
    """Lots that put exactly ``risk_amount`` at stake for a given stop distance."""
    per_lot = stop_distance_price(price, stop_pct) * GOLD_CONTRACT_SIZE
    return max(0.0, risk_amount / per_lot) if per_lot > 0 else 0.0


def cost_per_trade_r(
    spread_price: float, stop_distance: float, slippage_price: float = 0.0
) -> float:
    """Round-trip cost in R (a market entry pays the spread once, plus slippage)."""
    if stop_distance <= 0:
        raise ValueError("stop_distance must be > 0")
    return max(0.0, (spread_price + slippage_price) / stop_distance)


def trades_needed_for_significance(
    edge_r: float, *, sigma_r: float = DEFAULT_SIGMA_R, alpha_rank1: float = 0.003846
) -> float:
    """Independent trades required for a two-sided test to detect ``edge_r`` at ``alpha_rank1``."""
    if edge_r <= 0:
        raise ValueError("edge_r must be > 0")
    z = NormalDist().inv_cdf(1.0 - alpha_rank1 / 2.0)
    return (z * sigma_r / edge_r) ** 2


@dataclass(frozen=True)
class GoldTargetPlan:
    """What a hypothetical dollar target would require on gold — and what it would risk.

    This is a *sizing check for an edge that has already been validated*, never an input to trade
    selection. The direction of causation is fixed by the research contract:

        research discovers edge -> edge survives validation -> forward demo establishes evidence
        -> risk budget determines size -> a dollar target becomes an observation

    read it the other way (target -> lots -> force trades) and the target becomes a requirement that
    corrupts the experiment. ``observational_only`` is carried in the payload so that no downstream
    reader can mistake one for the other.
    """

    target_daily: float
    trades_per_day: float
    edge_r: float
    cost_r: float
    net_edge_r: float
    stop_pct: float
    price: float
    balance: float
    lots: float
    risk_per_trade: float
    risk_pct_of_balance: float
    expected_daily: float
    daily_risk: float
    trades_for_significance: float
    days_to_significance: float
    feasible: bool
    warnings: tuple[str, ...] = ()
    observational_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_daily": self.target_daily,
            "trades_per_day": self.trades_per_day,
            "edge_r": self.edge_r,
            "cost_r": self.cost_r,
            "net_edge_r": self.net_edge_r,
            "stop_pct": self.stop_pct,
            "price": self.price,
            "balance": self.balance,
            "lots": self.lots,
            "risk_per_trade": self.risk_per_trade,
            "risk_pct_of_balance": self.risk_pct_of_balance,
            "expected_daily": self.expected_daily,
            "daily_risk": self.daily_risk,
            "trades_for_significance": self.trades_for_significance,
            "days_to_significance": self.days_to_significance,
            "feasible": self.feasible,
            "warnings": list(self.warnings),
            "observational_only": self.observational_only,
            "note": (
                "a dollar target is an OUTPUT of a validated edge and a risk budget; "
                "it is never an input to trade selection, sizing or frequency"
            ),
        }


def plan_gold_target(
    *,
    target_daily: float,
    trades_per_day: float,
    edge_r: float,
    price: float,
    stop_pct: float = 0.5,
    spread_price: float = 0.26,
    slippage_price: float = 0.05,
    balance: float = 10_000.0,
    max_risk_pct_per_trade: float = 1.0,
    sigma_r: float = DEFAULT_SIGMA_R,
    alpha_rank1: float = 0.003846,
) -> GoldTargetPlan:
    """Solve for the size a target needs, given an edge, a frequency and the measured cost.

    ``feasible`` means only "the arithmetic works at or under the risk cap" — it says nothing about
    whether the edge exists, which is the part that has to be demonstrated in research.
    """
    if target_daily <= 0 or trades_per_day <= 0:
        raise ValueError("target_daily and trades_per_day must be > 0")
    stop_distance = stop_distance_price(price, stop_pct)
    cost_r = cost_per_trade_r(spread_price, stop_distance, slippage_price)
    net_edge = edge_r - cost_r
    if net_edge <= 0:
        return GoldTargetPlan(
            target_daily=target_daily,
            trades_per_day=trades_per_day,
            edge_r=edge_r,
            cost_r=cost_r,
            net_edge_r=net_edge,
            stop_pct=stop_pct,
            price=price,
            balance=balance,
            lots=0.0,
            risk_per_trade=0.0,
            risk_pct_of_balance=0.0,
            expected_daily=0.0,
            daily_risk=0.0,
            trades_for_significance=float("inf"),
            days_to_significance=float("inf"),
            feasible=False,
            warnings=(
                f"edge {edge_r:.3f}R does not cover cost {cost_r:.4f}R: no size is profitable",
            ),
        )
    required_risk = target_daily / (trades_per_day * net_edge)
    lots = lots_for_risk(price, stop_pct, required_risk)
    actual_risk = risk_per_trade(price, stop_pct, lots)
    risk_pct = (actual_risk / balance * 100.0) if balance > 0 else 0.0
    expected_daily = trades_per_day * actual_risk * net_edge
    warnings: list[str] = []
    if risk_pct > max_risk_pct_per_trade:
        warnings.append(
            f"risk {risk_pct:.2f}% per trade exceeds the {max_risk_pct_per_trade:.2f}% cap: out of "
            f"reach at this balance with this edge and frequency"
        )
    trades_needed = trades_needed_for_significance(
        net_edge, sigma_r=sigma_r, alpha_rank1=alpha_rank1
    )
    return GoldTargetPlan(
        target_daily=target_daily,
        trades_per_day=trades_per_day,
        edge_r=edge_r,
        cost_r=cost_r,
        net_edge_r=net_edge,
        stop_pct=stop_pct,
        price=price,
        balance=balance,
        lots=round(lots, 3),
        risk_per_trade=round(actual_risk, 2),
        risk_pct_of_balance=round(risk_pct, 3),
        expected_daily=round(expected_daily, 2),
        daily_risk=round(trades_per_day * actual_risk, 2),
        trades_for_significance=round(trades_needed, 1),
        days_to_significance=round(trades_needed / trades_per_day, 1),
        feasible=bool(net_edge > 0 and risk_pct <= max_risk_pct_per_trade),
        warnings=tuple(warnings),
    )


# ------------------------------------------------------------------ spread / cost audit


@dataclass(frozen=True)
class SpreadBucket:
    """Spread statistics for one grouping (e.g. one hour of the day)."""

    label: str
    bars: int
    mean_points: float
    median_points: float
    p90_points: float
    max_points: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "bars": self.bars,
            "mean_points": round(self.mean_points, 1),
            "median_points": round(self.median_points, 1),
            "p90_points": round(self.p90_points, 1),
            "max_points": round(self.max_points, 1),
        }


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def spread_by_hour(rows: Iterable[tuple[int, float]]) -> list[SpreadBucket]:
    """``(hour_utc, spread_points)`` pairs -> one bucket per hour, ordered 0..23."""
    grouped: dict[int, list[float]] = {}
    for hour, spread in rows:
        grouped.setdefault(int(hour), []).append(float(spread))
    buckets: list[SpreadBucket] = []
    for hour in sorted(grouped):
        values = sorted(grouped[hour])
        buckets.append(
            SpreadBucket(
                label=f"{hour:02d}:00",
                bars=len(values),
                mean_points=sum(values) / len(values),
                median_points=_quantile(values, 0.5),
                p90_points=_quantile(values, 0.9),
                max_points=values[-1],
            )
        )
    return buckets


def cost_table(
    stop_pcts: Iterable[float],
    *,
    price: float,
    spread_price: float,
    slippage_price: float = 0.05,
) -> list[dict[str, Any]]:
    """Cost in R for each stop size — the hurdle an edge must clear to be worth trading."""
    rows: list[dict[str, Any]] = []
    for stop_pct in stop_pcts:
        distance = stop_distance_price(price, stop_pct)
        cost_r = cost_per_trade_r(spread_price, distance, slippage_price)
        rows.append(
            {
                "stop_pct": stop_pct,
                "stop_distance_price": round(distance, 3),
                "risk_per_0.01_lot": round(risk_per_trade(price, stop_pct, 0.01), 2),
                "cost_r": round(cost_r, 5),
                "cost_pct_of_0.10r_edge": round(cost_r / 0.10 * 100.0, 1),
            }
        )
    return rows


__all__ = [
    "DEFAULT_SIGMA_R",
    "GOLD_CONTRACT_SIZE",
    "GoldTargetPlan",
    "SpreadBucket",
    "cost_per_trade_r",
    "cost_table",
    "lots_for_risk",
    "plan_gold_target",
    "risk_per_trade",
    "spread_by_hour",
    "stop_distance_price",
    "trades_needed_for_significance",
]
