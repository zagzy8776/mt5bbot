"""Research -> runtime promotion CLI (explicit, human-approved).

    python scripts/promote_candidate.py list [--report data/research_report.json]
    python scripts/promote_candidate.py approve --label "Donchian 30" --by "operator" \\
        [--config data/active_strategies.json] [--note "..."]

`list` shows every candidate with the research runner's own verdict and the metrics it produced —
this tool adds no gates and lowers none. `approve` writes an approved promotion; it refuses any
candidate the research did not validate, and the runtime only reads the file when it is pointed at
it (PROMOTION_CONFIG_PATH). Nothing here changes risk limits, gates or credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_platform.research.promotion import (  # noqa: E402
    approve_proposal,
    find_candidates,
    promoted_strategies,
)

DEFAULT_REPORT = "data/research_report.json"
DEFAULT_CONFIG = "data/active_strategies.json"


def _print_proposal(proposal) -> None:
    status = "ELIGIBLE" if proposal.eligible else "not eligible"
    print(f"[{status}] {proposal.label} -> {proposal.strategy} {proposal.params}")
    print(
        f"    {proposal.symbol} {proposal.timeframe} "
        f"{proposal.data_range[0]}..{proposal.data_range[1]}"
    )
    if proposal.metrics:
        print(
            "    IS trades={is_trades} PF={is_profit_factor} | OOS trades={oos_trades} "
            "PF={oos_profit_factor} return%={oos_return_pct}".format(**{**proposal.metrics})
        )
    if proposal.gates:
        print(f"    gates: {json.dumps(proposal.gates, sort_keys=True)}")
    if proposal.reasons:
        print(f"    reasons: {', '.join(proposal.reasons)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="promote validated research to the runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show candidates and their verdicts")
    listing.add_argument("--report", default=DEFAULT_REPORT)
    listing.add_argument("--symbol", default=None)
    listing.add_argument("--timeframe", default=None)
    listing.add_argument("--eligible-only", action="store_true")

    approve = sub.add_parser("approve", help="approve one validated candidate")
    approve.add_argument("--label", required=True)
    approve.add_argument("--by", required=True, help="who approves this (recorded verbatim)")
    approve.add_argument("--config", default=DEFAULT_CONFIG)
    approve.add_argument("--report", default=DEFAULT_REPORT)
    approve.add_argument("--note", default="")

    show = sub.add_parser("show", help="show currently approved promotions")
    show.add_argument("--config", default=DEFAULT_CONFIG)

    args = parser.parse_args(argv)

    if args.command == "list":
        proposals = find_candidates(args.report, symbol=args.symbol, timeframe=args.timeframe)
        if args.eligible_only:
            proposals = [p for p in proposals if p.eligible]
        for proposal in proposals:
            _print_proposal(proposal)
        eligible = sum(1 for p in proposals if p.eligible)
        print(f"\n{len(proposals)} candidate(s), {eligible} eligible for approval")
        return 0

    if args.command == "show":
        rows = promoted_strategies(args.config)
        if not rows:
            print(f"no approved promotions in {args.config}")
            return 0
        for row in rows:
            print(
                f"{row['version_id']} {row['strategy']} {row['params']} "
                f"({row['symbol']} {row['timeframe']}) approved_by={row['approved_by']}"
            )
        return 0

    proposals = find_candidates(args.report)
    match = [p for p in proposals if p.label.lower() == args.label.strip().lower()]
    if not match:
        print(f"no candidate labelled {args.label!r} in {args.report}")
        return 1
    proposal = match[0]
    try:
        entry = approve_proposal(
            proposal,
            config_path=args.config,
            approved_by=args.by,
            note=args.note,
        )
    except ValueError as exc:
        print(f"refused: {exc}")
        return 1
    print(f"approved {proposal.label} as {entry['version_id']} in {args.config}")
    print("the runtime uses it only when PROMOTION_CONFIG_PATH points at that file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
