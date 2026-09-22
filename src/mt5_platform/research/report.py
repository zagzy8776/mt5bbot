"""What the research pipeline currently knows, assembled for the control plane.

Reads the artefacts the research run leaves behind (report, manifest, holdout confirmations) and
combines them with the live guard rails and the forward record, so the dashboard can state the
truth in one place: how many candidates were searched, how many survived, whether anything is
promotable, how much forward evidence exists, and whether the final holdout is still sealed.

Nothing here computes statistics of its own — it reports what the run recorded, and says
``unknown``/``not_run`` instead of inventing a value when an artefact is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_REPORT_PATH = "data/research_report.json"
DEFAULT_MANIFEST_PATH = "data/research_manifest.json"


def _read_json(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_research_status(
    *,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    holdout_path: str | Path = "data/research_holdout.json",
    runtime_state: str = "",
    live_trading_enabled: bool = False,
    trading_mode: str = "",
    attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Research verdict + forward-evidence summary for the dashboard."""
    report = _read_json(report_path)
    manifest = _read_json(manifest_path) or (report or {}).get("manifest") or None
    holdout_records = (_read_json(holdout_path) or {}).get("confirmations", [])

    status: dict[str, Any] = {
        "report": {
            "available": report is not None,
            "generated_at": (report or {}).get("generated_at"),
            "path": str(report_path),
        },
        "candidates_searched": 0,
        "raw_gate_survivors": 0,
        "multiplicity_survivors": 0,
        "promotable": 0,
        "primary_correction": {},
        "sensitivity": {},
        "conclusion": {},
        "power": {},
        "holdout": {
            "sealed": None,
            "bars": 0,
            "window": "",
            "confirmation_records": len(holdout_records),
            "last_confirmed_at": holdout_records[-1]["confirmed_at"] if holdout_records else None,
        },
        "family": {"family_id": "", "hypothesis_version": "", "candidates": 0},
        "manifest": {"available": manifest is not None, "code_commit": "", "dataset_sha256": ""},
        "forward": {"strategy": [], "coverage": {}},
        "live_trading_enabled": bool(live_trading_enabled),
        "trading_mode": trading_mode,
        "runtime_state": runtime_state,
    }
    if report is not None:
        candidates = report.get("candidates") or []
        conclusion = report.get("conclusion") or {}
        family = report.get("family") or {}
        multiplicity = report.get("multiplicity") or {}
        holdout = report.get("holdout") or {}
        status["candidates_searched"] = len(candidates)
        status["raw_gate_survivors"] = len(conclusion.get("raw_gate_survivors") or [])
        status["multiplicity_survivors"] = len(multiplicity.get("survivors") or [])
        status["promotable"] = int(report.get("passed") or 0)
        status["primary_correction"] = {
            "method": multiplicity.get("method"),
            "alpha": multiplicity.get("alpha"),
        }
        status["sensitivity"] = {
            name: {"alpha": row.get("alpha"), "survivors": len(row.get("survivors") or [])}
            for name, row in (report.get("sensitivity") or {}).items()
        }
        status["conclusion"] = {
            "verdict": conclusion.get("verdict"),
            "interpretation": conclusion.get("interpretation"),
            "alpha_rank1": conclusion.get("alpha_rank1"),
        }
        status["power"] = {
            "median_n_effective": conclusion.get("median_n_effective"),
            "smallest_detectable_edge_r": conclusion.get("smallest_detectable_edge_r"),
            "trades_needed_for_0.05r_edge": conclusion.get("trades_needed_for_0.05r_edge"),
        }
        status["family"] = {
            "family_id": family.get("family_id", ""),
            "hypothesis_version": family.get("hypothesis_version", ""),
            "candidates": int(family.get("candidate_count") or len(candidates)),
        }
        if "holdout" in report:
            status["holdout"] = {
                "sealed": bool(holdout.get("sealed")),
                "bars": int(holdout.get("holdout_bars") or 0),
                "window": (
                    f"{str(holdout.get('holdout_start', ''))[:10]}"
                    f"..{str(holdout.get('holdout_end', ''))[:10]}"
                ),
                "confirmation_records": len(holdout_records),
                "last_confirmed_at": (
                    holdout_records[-1]["confirmed_at"] if holdout_records else None
                ),
            }
    if manifest is not None:
        status["manifest"] = {
            "available": True,
            "code_commit": str(manifest.get("code_commit", ""))[:12],
            "dataset_sha256": str(manifest.get("dataset_sha256", ""))[:12],
            "built_at": manifest.get("built_at"),
        }

    # Forward evidence: what the live ledger can actually say, bucket by bucket.
    attribution = attribution or {}
    rows = [
        {
            "strategy": bucket.get("key"),
            "trades": bucket.get("sample_size"),
            "grade": bucket.get("evidence_quality"),
            "expectancy_r": bucket.get("expectancy_r"),
            "win_rate": bucket.get("win_rate"),
            "verdict": bucket.get("verdict"),
        }
        for bucket in attribution.get("strategy") or []
    ]
    status["forward"] = {"strategy": rows, "coverage": attribution.get("coverage") or {}}
    return status


__all__ = ["DEFAULT_MANIFEST_PATH", "DEFAULT_REPORT_PATH", "build_research_status"]
