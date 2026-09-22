"""Research contract 8.1 guard: a silent change to the frozen values must fail the suite.

`docs/research-contract-8.1.md` is frozen. These tests pin the settings it declares, so that anyone
who changes α, the primary correction, the seed, the minimum samples or the holdout fraction will be
told to write a new contract version (8.2, 9.0, …) instead of quietly redefining what 8.1 meant.

Nothing here changes behaviour: it only refuses to let the constitution drift.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from mt5_platform.config import Settings
from mt5_platform.historical.attribution import coverage_summary, outcome_attribution
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.outcome_loader import evidence_status
from mt5_platform.research import manifest as manifest_module
from mt5_platform.research import multiplicity, runner
from mt5_platform.research.methodology import test_description as describe_test
from mt5_platform.research.report import build_research_status

CONTRACT_DOC = Path("docs/research-contract-8.1.md")


def test_frozen_defaults_are_unchanged() -> None:
    assert multiplicity.DEFAULT_ALPHA == 0.10
    assert multiplicity.DEFAULT_PERMUTATIONS == 2000
    assert multiplicity.DEFAULT_SEED == 42
    assert multiplicity.DEFAULT_MIN_TRADES == 20
    assert manifest_module.HOLDOUT_FRACTION == 0.15
    assert inspect.signature(runner.run_candidate).parameters["oos_fraction"].default == 0.3


def test_primary_correction_is_benjamini_hochberg_not_a_sensitivity_variant() -> None:
    names = {name for name in multiplicity.sensitivity({"a": 0.01}, alpha=0.10)}
    assert names == {"benjamini-hochberg", "benjamini-yekutieli", "bonferroni"}
    report = multiplicity.apply_multiplicity({"a": 0.001}, alpha=0.10)
    assert report.method == "benjamini-hochberg", "BH is the promotion gate, by contract"
    by = multiplicity.apply_multiplicity({"a": 0.001}, method="benjamini-yekutieli", alpha=0.10)
    assert by.method == "benjamini-yekutieli"


def test_test_is_two_sided_and_documented() -> None:
    described = describe_test(permutations=2000, seed=42, min_trades=20)
    assert described["sidedness"] == "two-sided"
    assert described["permutations"] == 2000 and described["seed"] == 42
    assert described["min_trades"] == 20
    assert "not evidence of absence" in described["interpretation"]


def test_runner_cli_defaults_match_the_contract() -> None:
    """Source-level guard: the CLI must keep offering the frozen values as its defaults."""
    source = inspect.getsource(runner.main)
    for expected in (
        "default=DEFAULT_ALPHA",
        'default="benjamini-hochberg"',
        "default=HOLDOUT_FRACTION",
        "default=2000",
        "default=42",
    ):
        assert expected in source, f"CLI default changed: {expected} missing"


def test_thresholds_lowered_stays_visible_everywhere() -> None:
    """The provenance flag future readers need: nothing here ever lowers a threshold."""
    empty = coverage_summary([])
    assert empty["thresholds_lowered"] is False
    assert empty["min_sample_weak"] == 10

    ledger = InMemoryHistoricalLedger()
    assert evidence_status(ledger, instrument="XAUUSDm")["thresholds_lowered"] is False

    status = build_research_status(
        report_path="data/does-not-exist.json",
        manifest_path="data/does-not-exist.json",
        holdout_path="data/does-not-exist.json",
        attribution={"strategy": [], "coverage": {"thresholds_lowered": False}},
    )
    assert status["forward"]["coverage"]["thresholds_lowered"] is False
    assert status["holdout"]["sealed"] is None, "no report means no claim about the holdout"


def test_attribution_buckets_report_the_flag_too() -> None:
    buckets = outcome_attribution([], group_by="strategy")
    assert buckets == []
    assert coverage_summary([])["thresholds_lowered"] is False


def test_live_trading_stays_disabled_by_default() -> None:
    settings = Settings()
    assert settings.live_trading_enabled is False
    assert settings.live_trading_acknowledged is False
    assert settings.is_live is False
    assert settings.trading_mode.value == "demo"


@pytest.mark.parametrize(
    "needle", ["FROZEN", "RESEARCH CONTRACT 8.1", "Holdout reuse", "prohibited"]
)
def test_contract_document_declares_the_frozen_rules(needle: str) -> None:
    assert CONTRACT_DOC.exists(), "the frozen contract must ship with the code"
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    assert needle in text
    assert "immutable" in text
