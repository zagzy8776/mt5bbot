"""Phase 8.1: hypothesis family records, the sealed holdout, and reproducible manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.research.manifest import (
    DatasetSplit,
    HypothesisRecord,
    build_manifest,
    dataset_digest,
    family_id_for,
    holdout_state,
    read_manifest,
    record_holdout_confirmation,
    slug,
    split_dataset,
    verify_manifest_dataset,
    write_manifest,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1.0


def _bars(count: int) -> list[_Bar]:
    return [
        _Bar(
            time=T0 + timedelta(minutes=15 * index),
            open=2500.0 + index * 0.1,
            high=2500.5 + index * 0.1,
            low=2499.5 + index * 0.1,
            close=2500.2 + index * 0.1,
        )
        for index in range(count)
    ]


# ----------------------------------------------------------- identity and records


def test_slug_and_family_id_are_stable_and_specific() -> None:
    assert slug("Donchian 50") == "donchian-50"
    assert slug("SMA 10/50") == "sma-10-50"
    assert slug("   ") == "unnamed"
    base = family_id_for(
        symbol="XAUUSDm", timeframe="M15", hypothesis_version="1", code_commit="abc"
    )
    same = family_id_for(
        symbol="XAUUSDm", timeframe="M15", hypothesis_version="1", code_commit="abc"
    )
    other_version = family_id_for(
        symbol="XAUUSDm", timeframe="M15", hypothesis_version="2", code_commit="abc"
    )
    other_commit = family_id_for(
        symbol="XAUUSDm", timeframe="M15", hypothesis_version="1", code_commit="def"
    )
    assert base == same and base.startswith("fam_")
    assert base != other_version and base != other_commit


def test_hypothesis_record_captures_what_was_searched() -> None:
    record = HypothesisRecord(
        candidate_id="donchian-30",
        family_id="fam_x",
        label="Donchian 30",
        symbol="XAUUSDm",
        timeframe="M15",
        regime_definition="trending",
        parameter_set={"lookback": 30},
        hypothesis_version="1",
    )
    payload = record.to_dict()
    assert payload["candidate_id"] == "donchian-30"
    assert payload["regime_definition"] == "trending"
    assert payload["parameter_set"] == {"lookback": 30}
    assert payload["hypothesis_version"] == "1"
    assert set(payload) == {
        "candidate_id",
        "family_id",
        "label",
        "symbol",
        "timeframe",
        "regime_definition",
        "parameter_set",
        "hypothesis_version",
    }


# ---------------------------------------------------------------- dataset splitting


def test_split_seals_the_most_recent_bars() -> None:
    bars = _bars(100)
    research, split, holdout = split_dataset(bars, holdout_fraction=0.1, validation_fraction=0.5)

    assert split.total_bars == 100 and split.holdout_bars == 10 and split.research_bars == 90
    assert len(research) == 90 and len(holdout) == 10
    assert research[-1].time < holdout[0].time, "the holdout is the most recent slice"
    assert split.holdout_start == holdout[0].time.isoformat()
    assert split.holdout_end == holdout[-1].time.isoformat()
    assert split.sealed is True
    assert split.discovery_bars == 45 and split.validation_bars == 45
    assert split.holdout_sha256 == dataset_digest(holdout)
    assert split.to_dict()["role"]["final_holdout"].startswith("sealed:")


def test_split_can_be_disabled_explicitly_and_validates_input() -> None:
    bars = _bars(40)
    _research, split, holdout = split_dataset(bars, holdout_fraction=0.0)
    assert split.holdout_bars == 0 and split.sealed is False and holdout == []
    with pytest.raises(ValueError):
        split_dataset(bars, holdout_fraction=1.0)


def test_dataset_digest_detects_any_change() -> None:
    bars = _bars(20)
    original = dataset_digest(bars)
    assert dataset_digest(bars) == original
    bars[5].close += 0.01
    assert dataset_digest(bars) != original


# ------------------------------------------------------------- holdout confirmation


def test_holdout_confirmation_is_recorded_once(tmp_path) -> None:
    path = tmp_path / "holdout.json"
    first = record_holdout_confirmation(
        family_id="fam_a",
        dataset_sha256="d" * 64,
        holdout_sha256="h" * 64,
        survivors=["Donchian 50"],
        results={"Donchian 50": {"oos_mean_r": 0.2}},
        path=path,
    )
    assert first["survivors"] == ["Donchian 50"]
    assert "not another dataset to optimise against" in first["interpretation"]
    assert len(holdout_state(path)["confirmations"]) == 1

    with pytest.raises(RuntimeError, match="already confirmed"):
        record_holdout_confirmation(
            family_id="fam_a",
            dataset_sha256="d" * 64,
            holdout_sha256="h" * 64,
            survivors=[],
            path=path,
        )

    forced = record_holdout_confirmation(
        family_id="fam_a",
        dataset_sha256="d" * 64,
        holdout_sha256="h" * 64,
        survivors=[],
        path=path,
        force=True,
    )
    assert forced["survivors"] == []
    records = holdout_state(path)["confirmations"]
    assert len(records) == 1, "a forced re-run replaces the void record rather than piling up"

    record_holdout_confirmation(
        family_id="fam_b",
        dataset_sha256="d" * 64,
        holdout_sha256="h" * 64,
        survivors=[],
        path=path,
    )
    assert len(holdout_state(path)["confirmations"]) == 2, "a different family is a different act"


def test_holdout_state_handles_missing_and_corrupt_files(tmp_path) -> None:
    assert holdout_state(tmp_path / "nope.json") == {"confirmations": []}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert holdout_state(corrupt) == {"confirmations": []}


# ----------------------------------------------------------------------- manifest


def _manifest(tmp_path):
    bars = _bars(60)
    research, split, _holdout = split_dataset(bars, holdout_fraction=0.1)
    return build_manifest(
        dataset_path=tmp_path / "bars.csv",
        all_bars=bars,
        research_bars=research,
        symbol="XAUUSDm",
        timeframe="M15",
        costs={"spread_price": 0.26, "slippage_price": 0.05, "commission_per_lot": 0.0},
        backtest={"starting_balance": 100_000.0, "risk_pct": 1.0, "max_volume": 0.1,
                  "max_exposure_pct": 300.0},
        family={
            "family_id": "fam_test",
            "hypothesis_version": "1",
            "candidates": [
                HypothesisRecord(
                    candidate_id="donchian-30",
                    family_id="fam_test",
                    label="Donchian 30",
                    symbol="XAUUSDm",
                    timeframe="M15",
                    parameter_set={"lookback": 30},
                ).to_dict()
            ],
        },
        statistics={"permutations": 2000, "seed": 42, "h0": "expected per-trade R <= 0"},
        correction={"primary": "benjamini-hochberg", "alpha": 0.1},
        holdout=split.to_dict(),
        directory=tmp_path,
    )


def test_manifest_captures_the_experiment(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    payload = manifest.to_dict()
    for key in (
        "dataset_sha256",
        "date_range",
        "symbol",
        "timeframe",
        "spread_price",
        "slippage_price",
        "candidates",
        "statistics",
        "correction",
        "holdout",
        "code_commit",
    ):
        assert key in payload, f"manifest is missing {key}"
    assert payload["symbol"] == "XAUUSDm" and payload["timeframe"] == "M15"
    assert payload["spread_price"] == 0.26 and payload["slippage_price"] == 0.05
    assert payload["correction"]["alpha"] == 0.1
    assert payload["statistics"]["seed"] == 42
    assert payload["candidates"][0]["candidate_id"] == "donchian-30"

    arguments = manifest.to_run_arguments()
    assert arguments["alpha"] == 0.1
    assert arguments["method"] == "benjamini-hochberg"
    assert arguments["seed"] == 42 and arguments["permutations"] == 2000
    assert arguments["dataset_sha256"] == manifest.dataset_sha256


def test_manifest_round_trip_and_dataset_verification(tmp_path) -> None:
    bars = _bars(60)
    manifest = _manifest(tmp_path)
    path = write_manifest(manifest, tmp_path / "manifest.json")

    reloaded = read_manifest(path)
    assert reloaded.family_id == manifest.family_id
    assert reloaded.dataset_sha256 == manifest.dataset_sha256
    assert reloaded.to_dict()["candidates"] == manifest.to_dict()["candidates"]
    assert json.loads(path.read_text(encoding="utf-8"))["dataset_path"].endswith("bars.csv")

    matches, detail = verify_manifest_dataset(reloaded, bars)
    assert matches is True and "matches" in detail

    bars[0].close += 1.0
    matches, detail = verify_manifest_dataset(reloaded, bars)
    assert matches is False and "mismatch" in detail

    with pytest.raises(FileNotFoundError):
        read_manifest(tmp_path / "missing.json")


def test_split_record_is_serializable() -> None:
    _research, split, _holdout = split_dataset(_bars(30), holdout_fraction=0.2)
    payload = split.to_dict()
    assert isinstance(split, DatasetSplit)
    assert payload["holdout_bars"] == 6 and payload["sealed"] is True
    assert json.dumps(payload)  # nothing exotic that json cannot carry
