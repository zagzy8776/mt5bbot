"""Contract 8.2: the pre-registered scalp family must not drift after being measured."""

from __future__ import annotations

import re
from pathlib import Path

RUNNER = Path("src/mt5_platform/research/runner.py").read_text(encoding="utf-8")
CONTRACT = Path("docs/research-contract-8.2.md")


def test_scalp_family_size_is_the_preregistered_twelve() -> None:
    """The 12 candidates are the search. Trimming them would loosen the BH threshold."""
    block = RUNNER.split("# F. Contract 8.2", 1)[1].split("]", 1)[0]
    labels = re.findall(r'\(\s*"(Scalp [^"]+)"', block)
    assert len(labels) == 12, f"family size drifted: {labels}"
    assert len(set(labels)) == 12, "duplicate candidate labels would understate the family"


def test_scalp_family_uses_wide_stops_from_the_cost_audit() -> None:
    """Sub-0.15% stops surrender ~half of a 0.10R edge to gold's ~240-point spread."""
    block = RUNNER.split("# F. Contract 8.2", 1)[1].split("]", 1)[0]
    stops = [float(value) for value in re.findall(r'"stop_loss_pct": ([0-9.]+)', block)]
    assert len(stops) == 12
    assert min(stops) >= 0.3, "a tighter stop was pre-registered against the audit's advice"


def test_contract_8_2_exists_and_names_the_family_and_alpha() -> None:
    """A new family needs its own pre-registered contract, not an edit to 8.1."""
    assert CONTRACT.exists(), "contract 8.2 must exist and stay readable"
    text = CONTRACT.read_text(encoding="utf-8")
    assert "fam_gold_scalp_m15" in text
    assert "PRE-REGISTERED" in text
    assert "Candidate count (declared before the run)" in text
