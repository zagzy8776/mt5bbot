"""Gold-only target plan + cost audit: what $X/day actually requires, and what it risks.

    python scripts/gold_target.py --target 20 --trades-per-day 4 --edge-r 0.10 --balance 10000
    python scripts/gold_target.py --target 20 --edge-r 0.10 --audit-csv data/xauusd_m15.csv

The numbers come from `mt5_platform.research.targets`; this script only formats them. Nothing here
changes a gate, a size or a setting — it tells you what the arithmetic requires.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_platform.research.targets import (  # noqa: E402
    cost_table,
    plan_gold_target,
    spread_by_hour,
)

DEFAULT_CSV = "data/xauusd_m15.csv"


def _read_spreads(path: Path) -> list[tuple[int, float]]:
    rows: list[tuple[int, float]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = row.get("spread")
            if raw in (None, ""):
                continue
            stamp = row.get("time", "")
            hour = int(stamp[11:13]) if len(stamp) >= 13 else 0
            rows.append((hour, float(raw)))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gold target planner and cost audit")
    parser.add_argument("--target", type=float, default=20.0, help="daily $ target")
    parser.add_argument("--trades-per-day", type=float, default=4.0)
    parser.add_argument("--edge-r", type=float, default=0.10, help="gross edge per trade, in R")
    parser.add_argument("--price", type=float, default=4324.0)
    parser.add_argument("--stop-pct", type=float, default=0.5)
    parser.add_argument("--spread-price", type=float, default=0.26)
    parser.add_argument("--slippage-price", type=float, default=0.05)
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--max-risk-pct", type=float, default=1.0)
    parser.add_argument(
        "--audit-csv", default="", help="CSV with a spread column, for the cost audit"
    )
    parser.add_argument("--json", default="", help="write the full result as JSON to this path")
    args = parser.parse_args(argv)

    plan = plan_gold_target(
        target_daily=args.target,
        trades_per_day=args.trades_per_day,
        edge_r=args.edge_r,
        price=args.price,
        stop_pct=args.stop_pct,
        spread_price=args.spread_price,
        slippage_price=args.slippage_price,
        balance=args.balance,
        max_risk_pct_per_trade=args.max_risk_pct,
    )
    print(f"GOLD TARGET PLAN  ${args.target:.2f}/day  @ {args.price:.2f}  stop {args.stop_pct}%")
    print(f"  edge {plan.edge_r:.3f}R - cost {plan.cost_r:.4f}R = net {plan.net_edge_r:.4f}R")
    print(f"  lots {plan.lots:.3f} -> risk ${plan.risk_per_trade:.2f}/trade "
          f"({plan.risk_pct_of_balance:.2f}% of ${args.balance:,.0f})")
    print(f"  expected ${plan.expected_daily:.2f}/day, daily risk exposure ${plan.daily_risk:.2f}")
    print(f"  needs ~{plan.trades_for_significance:,.0f} independent trades "
          f"(~{plan.days_to_significance:,.0f} days) before this edge is "
          f"distinguishable from noise")
    print(f"  feasible at this risk cap: {plan.feasible}")
    for warning in plan.warnings:
        print(f"  WARNING: {warning}")

    payload: dict = {"plan": plan.to_dict()}
    print("\nCOST HURDLE BY STOP SIZE (spread + slippage as a fraction of R)")
    table = cost_table(
        (0.15, 0.2, 0.3, 0.5, 1.0),
        price=args.price,
        spread_price=args.spread_price,
        slippage_price=args.slippage_price,
    )
    for row in table:
        print(
            f"  stop {row['stop_pct']:.2f}% = {row['stop_distance_price']:7.2f} price | "
            f"risk/0.01 lot ${row['risk_per_0.01_lot']:6.2f} | cost {row['cost_r']:.4f}R "
            f"({row['cost_pct_of_0.10r_edge']:.0f}% of a 0.10R edge)"
        )
    payload["cost_table"] = table

    if args.audit_csv:
        path = Path(args.audit_csv)
        if not path.exists():
            print(f"\naudit skipped: {path} not found")
        else:
            rows = _read_spreads(path)
            buckets = spread_by_hour(rows)
            print(f"\nGOLD SPREAD BY HOUR (UTC), from {path} ({len(rows)} bars)")
            cheapest = min(buckets, key=lambda b: b.mean_points) if buckets else None
            for bucket in buckets:
                print(
                    f"  {bucket.label}  bars={bucket.bars:5d}  mean={bucket.mean_points:7.1f}  "
                    f"median={bucket.median_points:7.1f}  p90={bucket.p90_points:7.1f}  "
                    f"max={bucket.max_points:7.1f}"
                )
            if cheapest:
                print(f"  cheapest hour: {cheapest.label} (mean {cheapest.mean_points:.1f} pts)")
            payload["spread_by_hour"] = [bucket.to_dict() for bucket in buckets]

    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
