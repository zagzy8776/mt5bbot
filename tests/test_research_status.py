"""Phase 8.1: the research status endpoint the dashboard reads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.config import Settings, TradingMode
from mt5_platform.research.report import build_research_status


def _report() -> dict:
    return {
        "generated_at": "2026-09-22T15:22:09+00:00",
        "passed": 0,
        "multiplicity": {"method": "benjamini-hochberg", "alpha": 0.1, "survivors": []},
        "sensitivity": {
            "benjamini-hochberg": {"alpha": 0.1, "survivors": []},
            "benjamini-yekutieli": {"alpha": 0.1, "survivors": []},
            "bonferroni": {"alpha": 0.05, "survivors": []},
        },
        "family": {"family_id": "fam_x", "hypothesis_version": "1", "candidate_count": 26},
        "holdout": {
            "sealed": True,
            "holdout_bars": 3544,
            "holdout_start": "2026-07-29T06:30:00+00:00",
            "holdout_end": "2026-09-21T20:45:00+00:00",
        },
        "conclusion": {
            "verdict": "no_candidate_survived",
            "interpretation": "failure to reject the null is not evidence of absence",
            "median_n_effective": 166.1,
            "smallest_detectable_edge_r": 0.1805,
            "trades_needed_for_0.05r_edge": 2164.8,
            "raw_gate_survivors": ["Donchian 20", "Donchian 30"],
        },
        "manifest": {"code_commit": "3a1c9ac1234", "dataset_sha256": "abc123def456"},
        "candidates": [{"name": "Donchian 20"}, {"name": "Donchian 30"}],
    }


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_research_status_is_explicit_when_nothing_was_run(tmp_path: Path) -> None:
    status = build_research_status(
        report_path=tmp_path / "missing.json",
        manifest_path=tmp_path / "missing_manifest.json",
        holdout_path=tmp_path / "missing_holdout.json",
        trading_mode="demo",
    )
    assert status["report"]["available"] is False
    assert status["candidates_searched"] == 0
    assert status["holdout"]["sealed"] is None, "no report means no claim about the holdout"
    assert status["manifest"]["available"] is False
    assert status["live_trading_enabled"] is False
    assert status["forward"] == {"strategy": [], "coverage": {}}


def test_research_status_summarises_the_verdict_and_forward_evidence(tmp_path: Path) -> None:
    report = _write(tmp_path, "report.json", _report())
    manifest = _write(
        tmp_path, "manifest.json", {"code_commit": "3a1c9ac1234", "dataset_sha256": "abc"}
    )
    holdout = _write(
        tmp_path,
        "holdout.json",
        {"confirmations": [{"confirmed_at": "2026-09-22T16:00:00+00:00"}]},
    )

    attribution = {
        "strategy": [
            {
                "key": "breakout",
                "sample_size": 7,
                "evidence_quality": "insufficient",
                "expectancy_r": 0.18,
                "win_rate": 0.57,
                "verdict": "insufficient_evidence",
            }
        ],
        "coverage": {"measured": 0, "unmeasured": 1, "closed_trades": 7},
    }
    status = build_research_status(
        report_path=report,
        manifest_path=manifest,
        holdout_path=holdout,
        runtime_state="running",
        trading_mode="demo",
        attribution=attribution,
    )

    assert status["candidates_searched"] == 2
    assert status["raw_gate_survivors"] == 2 and status["multiplicity_survivors"] == 0
    assert status["promotable"] == 0
    assert status["primary_correction"] == {"method": "benjamini-hochberg", "alpha": 0.1}
    assert set(status["sensitivity"]) == {"benjamini-hochberg", "benjamini-yekutieli", "bonferroni"}
    assert status["conclusion"]["verdict"] == "no_candidate_survived"
    assert status["power"]["smallest_detectable_edge_r"] == 0.1805
    assert status["family"]["family_id"] == "fam_x" and status["family"]["candidates"] == 26
    assert status["holdout"]["sealed"] is True and status["holdout"]["bars"] == 3544
    assert status["holdout"]["confirmation_records"] == 1
    assert status["manifest"]["code_commit"] == "3a1c9ac1234"
    assert status["forward"]["strategy"][0]["strategy"] == "breakout"
    assert status["forward"]["strategy"][0]["trades"] == 7
    assert status["forward"]["coverage"]["measured"] == 0
    assert status["runtime_state"] == "running"


def test_legacy_report_without_a_holdout_block_says_unknown(tmp_path: Path) -> None:
    """A report from before the sealed holdout existed must not be read as 'not sealed'."""
    legacy = _report()
    legacy.pop("holdout")
    legacy.pop("sensitivity")
    legacy.pop("family")
    report = _write(tmp_path, "legacy.json", legacy)
    status = build_research_status(
        report_path=report,
        manifest_path=tmp_path / "none.json",
        holdout_path=tmp_path / "none.json",
        trading_mode="demo",
    )
    assert status["holdout"]["sealed"] is None, "unknown, not 'unsealed'"
    assert status["sensitivity"] == {}
    assert status["candidates_searched"] == 2  # what the report did record is still used
    # the report embeds its own manifest, so provenance survives a missing manifest file
    assert status["manifest"]["available"] is True
    assert status["manifest"]["code_commit"] == "3a1c9ac1234"


def test_research_status_endpoint_serves_the_summary(tmp_path: Path) -> None:
    report = _write(tmp_path, "report.json", _report())
    manifest = _write(
        tmp_path, "manifest.json", {"code_commit": "3a1c9ac1234", "dataset_sha256": "abc"}
    )
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        research_report_path=str(report),
        research_manifest_path=str(manifest),
        research_holdout_path=str(tmp_path / "holdout.json"),
    )
    app = create_app(settings)

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/research/status")
            assert response.status_code == 200
            body = response.json()
            assert body["multiplicity_survivors"] == 0 and body["promotable"] == 0
            assert body["holdout"]["sealed"] is True
            assert body["trading_mode"] == "demo" and body["live_trading_enabled"] is False
            assert body["forward"]["strategy"] == [], "no closed trades yet, so nothing claimed"

    asyncio.run(run())
