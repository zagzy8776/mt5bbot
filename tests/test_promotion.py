"""Phase 6: research -> runtime promotion. Explicit, evidence-bound, and never automatic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mt5_platform.config import Settings
from mt5_platform.research.promotion import (
    approve_proposal,
    build_promoted_strategies,
    find_candidates,
    promoted_strategies,
    read_active_config,
    strategy_for_label,
)
from mt5_platform.signals import build_signal_engine
from mt5_platform.storage import InMemoryMarketDataStore

REPORT = Path("data/research_report.json")
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "name": "Donchian 30",
        "strategy": "breakout",
        "params": {"lookback": 30},
        "symbol": "XAUUSDm",
        "timeframe": "M15",
        "data_range": ["2025-09-21", "2026-09-21"],
        "validation_passed": True,
        "rejection_reasons": [],
        # multiplicity control (research/multiplicity.py): a candidate is only promotable when the
        # family-corrected verdict says it is a survivor.
        "multiplicity_survivor": True,
        "p_value": 0.0008,
        "p_value_adjusted": 0.009,
        "oos_mean_r": 0.21,
        "is_trades": 668,
        "oos_trades": 249,
        "oos_profit_factor": 1.12,
        "oos_return_pct": 4.1,
        "wf_pass_rate": 1.0,
        "mc_pct_profitable": 0.92,
    }
    candidate.update(overrides)
    return candidate


def _report(tmp_path: Path, candidates: list[dict[str, Any]]) -> Path:
    path = tmp_path / "research_report.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "symbol": "XAUUSDm",
                "timeframe": "M15",
                "passed": sum(1 for c in candidates if c.get("validation_passed")),
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------- eligibility


def test_label_resolution_covers_the_research_labels() -> None:
    assert strategy_for_label("Donchian 30") == "breakout"
    assert strategy_for_label("SMA 10/50") == "sma_crossover"
    assert strategy_for_label("MeanRev w20 z2.0") == "mean_reversion"
    assert strategy_for_label("Momentum lb10 0.5%") == "momentum"
    assert strategy_for_label("ATR 14") == "atr_breakout"
    assert strategy_for_label("something else") is None


def test_report_candidates_are_offered_with_the_research_verdict(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        [
            _candidate(name="Survivor 1"),
            _candidate(name="Marginal 1", multiplicity_survivor=False, p_value=0.04),
            _candidate(name="Broken 1", validation_passed=False, rejection_reasons=["OOS PF<0.9"]),
        ],
    )
    proposals = find_candidates(report, symbol="XAUUSDm", timeframe="M15")

    assert len(proposals) == 3, "every candidate is offered; nothing is silently filtered away"
    by_label = {p.label: p for p in proposals}
    assert by_label["Survivor 1"].eligible is True and by_label["Survivor 1"].reasons == []
    assert by_label["Marginal 1"].eligible is False
    assert "fails_multiplicity_control" in by_label["Marginal 1"].reasons
    assert by_label["Broken 1"].eligible is False
    assert "research_validation_failed" in by_label["Broken 1"].reasons
    for proposal in proposals:
        assert proposal.provenance["report_sha256"]
        assert proposal.gates["validation_passed"] is not None


def test_legacy_report_without_multiplicity_cannot_be_promoted(tmp_path: Path) -> None:
    """A report from before multiplicity control is not promotable: the research must be re-run."""
    legacy = _candidate()
    legacy.pop("multiplicity_survivor")
    legacy.pop("p_value")
    legacy.pop("p_value_adjusted")
    proposals = find_candidates(_report(tmp_path, [legacy]))
    assert proposals[0].eligible is False
    assert proposals[0].reasons == ["multiplicity_not_evaluated:rerun_research"]
    with pytest.raises(ValueError, match="not eligible"):
        approve_proposal(
            proposals[0], config_path=tmp_path / "active.json", approved_by="operator", now=NOW
        )


def test_eligibility_reuses_the_research_gates_and_lowers_none(tmp_path: Path) -> None:
    failed = find_candidates(_report(tmp_path, [_candidate(validation_passed=False)]))
    assert failed[0].eligible is False
    assert "research_validation_failed" in failed[0].reasons

    rejected = find_candidates(
        _report(tmp_path, [_candidate(rejection_reasons=["oos_profit_factor_below_one"])])
    )
    assert rejected[0].eligible is False
    assert any(reason.startswith("rejection_reason:") for reason in rejected[0].reasons)

    unknown = find_candidates(_report(tmp_path, [_candidate(name="Mystery 1", strategy="")]))
    assert unknown[0].eligible is False and "unknown_strategy_for_label" in unknown[0].reasons

    bad_params = find_candidates(
        _report(tmp_path, [_candidate(params={"lookback": 1})])  # breakout requires >= 2
    )
    assert bad_params[0].eligible is False
    assert any(reason.startswith("invalid_params:") for reason in bad_params[0].reasons)

    other_symbol = find_candidates(_report(tmp_path, [_candidate()]), symbol="EURUSDm")
    assert other_symbol[0].eligible is False
    assert other_symbol[0].reasons[0].startswith("symbol_mismatch")


def test_report_carrying_the_factory_key_wins_over_the_label(tmp_path: Path) -> None:
    proposals = find_candidates(
        _report(
            tmp_path,
            [
                _candidate(
                    name="Donchian 30",
                    strategy="sma_crossover",
                    params={"fast_period": 5, "slow_period": 20},
                )
            ],
        )
    )
    assert proposals[0].strategy == "sma_crossover"


# ------------------------------------------------------------------------ approval


def test_approval_refuses_ineligible_candidates_and_anonymous_approvers(tmp_path: Path) -> None:
    ineligible = find_candidates(_report(tmp_path, [_candidate(validation_passed=False)]))[0]
    with pytest.raises(ValueError, match="not eligible"):
        approve_proposal(
            ineligible, config_path=tmp_path / "active.json", approved_by="operator", now=NOW
        )

    eligible = find_candidates(_report(tmp_path, [_candidate()]))[0]
    with pytest.raises(ValueError, match="approved_by"):
        approve_proposal(
            eligible, config_path=tmp_path / "active.json", approved_by="   ", now=NOW
        )
    assert not (tmp_path / "active.json").exists(), "a refused approval writes nothing"


def test_approval_records_the_evidence_and_the_version(tmp_path: Path) -> None:
    config = tmp_path / "active_strategies.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    entry = approve_proposal(
        proposal, config_path=config, approved_by="operator", note="reviewed", now=NOW
    )
    assert entry["version_id"].startswith("cfg_")
    assert entry["approved_at"] == NOW.isoformat()
    assert entry["approved_by"] == "operator"
    assert entry["gates"]["validation_passed"] is True
    assert entry["metrics"]["oos_trades"] == 249
    assert entry["metrics"]["multiplicity_survivor"] is True
    assert entry["metrics"]["p_value_adjusted"] == 0.009
    assert entry["provenance"]["report_sha256"] == proposal.provenance["report_sha256"]

    stored = read_active_config(config)["promotions"]
    assert len(stored) == 1 and stored[0]["label"] == "Donchian 30"


def test_approval_is_idempotent_for_the_same_candidate(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    first = approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)
    second = approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)
    rows = read_active_config(config)["promotions"]
    assert len(rows) == 1 and rows[0]["version_id"] == second["version_id"]
    assert first["version_id"] != second["version_id"], "a re-approval is a new version"


def test_promotion_file_has_no_risk_or_credential_levers(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)
    payload = json.loads(config.read_text(encoding="utf-8"))
    allowed = {
        "version_id",
        "strategy",
        "params",
        "symbol",
        "timeframe",
        "label",
        "approved_by",
        "approved_at",
        "note",
        "data_range",
        "metrics",
        "gates",
        "provenance",
    }
    assert set(payload["promotions"][0]) == allowed
    assert set(payload) <= {"promotions", "updated_at"}
    text = config.read_text(encoding="utf-8").lower()
    for forbidden in ("max_risk", "max_daily_loss", "max_spread", "password", "token", "live_"):
        assert forbidden not in text, f"promotion must not carry {forbidden}"


# ------------------------------------------------------------- runtime integration


def test_runtime_uses_the_promoted_strategy_and_stamps_the_version(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    entry = approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)

    engine = build_signal_engine(
        Settings(default_symbol="XAUUSDm", promotion_config_path=str(config)),
        InMemoryMarketDataStore(),
    )
    strategy = engine.get_strategy("breakout")
    assert strategy is not None
    assert strategy.lookback == 30  # the approved parameters, not the factory default
    assert strategy.version == entry["version_id"], "outcomes trace back to the approval"


def test_runtime_ignores_promotions_when_not_pointed_at_them(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)

    engine = build_signal_engine(
        Settings(default_symbol="XAUUSDm", strategies="breakout"),
        InMemoryMarketDataStore(),
    )
    strategy = engine.get_strategy("breakout")
    assert strategy is not None and strategy.lookback == 20  # factory default, no promotion


def test_promotions_do_not_apply_to_another_symbol(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    proposal = find_candidates(_report(tmp_path, [_candidate()]))[0]
    approve_proposal(proposal, config_path=config, approved_by="operator", now=NOW)

    assert promoted_strategies(config, symbol="XAUUSDm") != []
    assert promoted_strategies(config, symbol="EURUSDm") == []
    engine = build_signal_engine(
        Settings(default_symbol="EURUSDm", promotion_config_path=str(config)),
        InMemoryMarketDataStore(),
    )
    assert engine.get_strategy("breakout").version == "1.0.0"  # unpromoted default


def test_bad_promotion_entries_are_skipped_not_fatal(tmp_path: Path) -> None:
    config = tmp_path / "active.json"
    config.write_text(
        json.dumps(
            {
                "promotions": [
                    {"strategy": "not_a_strategy", "params": {}, "symbol": "XAUUSDm"},
                    {"strategy": "breakout", "params": {"lookback": 1}, "symbol": "XAUUSDm"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert build_promoted_strategies(config, symbol="XAUUSDm") == []


def test_missing_or_corrupt_promotion_file_is_empty_not_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert read_active_config(missing) == {"promotions": []}
    assert build_promoted_strategies(missing) == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_active_config(corrupt) == {"promotions": []}
