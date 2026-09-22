"""Hypothesis family records, the sealed final holdout, and a reproducible research manifest.

Three problems this module exists to solve.

1. *What exactly was searched?* Every candidate is recorded with its ``family_id``,
   ``candidate_id``, symbol, timeframe, regime definition, parameter set and hypothesis version, so
   a multiplicity report can state the search it corrected for instead of implying it.
2. *Has this data been used before?* The dataset is split into DISCOVERY / VALIDATION / FINAL
   HOLDOUT. Selection may only use discovery + validation; the holdout slice is hashed and sealed,
   and a confirmation run on it is recorded once and refuses to be overwritten without an explicit
   force. Once you look at the holdout it stops being a holdout, so looking is logged.
3. *Can this run be reproduced?* The manifest captures the dataset hash, date range, symbol,
   timeframe, cost assumptions, candidate family, seeds, correction method, alpha and code commit,
   and the runner can be re-run from it.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOLDOUT_FRACTION = 0.15  # of the bars, most recent, sealed
DEFAULT_CONFIRMATION_PATH = "data/research_holdout.json"


def slug(text: str) -> str:
    """Stable identifier for a label ("Donchian 50" -> "donchian-50")."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text.strip().lower())
    return "-".join(part for part in cleaned.split("-") if part) or "unnamed"


def family_id_for(
    *, symbol: str, timeframe: str, hypothesis_version: str, code_commit: str = ""
) -> str:
    """Identifier for one search family: same code, symbol, timeframe and hypothesis version."""
    raw = "|".join(
        (symbol.strip(), timeframe.strip(), hypothesis_version.strip(), code_commit[:12])
    )
    return f"fam_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class HypothesisRecord:
    """One hypothesis that was searched, recorded so the family is auditable after the fact."""

    candidate_id: str
    family_id: str
    label: str
    symbol: str
    timeframe: str
    regime_definition: str = ""
    parameter_set: dict[str, Any] = field(default_factory=dict)
    hypothesis_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family_id": self.family_id,
            "label": self.label,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "regime_definition": self.regime_definition,
            "parameter_set": dict(self.parameter_set),
            "hypothesis_version": self.hypothesis_version,
        }


def code_commit(directory: str | Path = ".") -> str:
    """Current git commit (empty string when unavailable — never fabricated)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - no git, no commit; the manifest says so
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def dataset_digest(bars: Any) -> str:
    """Content hash of a bar slice (time + OHLCV), so 'same data' is verifiable, not assumed."""
    digest = hashlib.sha256()
    for bar in bars:
        line = f"{bar.time.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetSplit:
    """DISCOVERY + VALIDATION (selection may use these) and a sealed FINAL HOLDOUT."""

    total_bars: int
    discovery_bars: int
    validation_bars: int
    research_bars: int
    holdout_bars: int
    holdout_start: str = ""
    holdout_end: str = ""
    holdout_sha256: str = ""
    sealed: bool = True
    holdout_fraction: float = HOLDOUT_FRACTION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role"] = {
            "discovery": "develop hypotheses",
            "validation": "select candidates (IS/OOS split lives here)",
            "final_holdout": (
                "sealed: one confirmation run after selection; it can never flow back into "
                "candidate development"
            ),
        }
        return payload


def split_dataset(
    bars: Any,
    *,
    holdout_fraction: float = HOLDOUT_FRACTION,
    validation_fraction: float = 0.25,
) -> tuple[list[Any], DatasetSplit, list[Any]]:
    """Split bars into (research, split-record, sealed holdout). The most recent bars are sealed."""
    rows = list(bars)
    if not 0.0 <= holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in [0, 1)")
    holdout_count = int(len(rows) * holdout_fraction)
    research_count = len(rows) - holdout_count
    research = rows[:research_count]
    holdout = rows[research_count:]
    validation_bars = int(len(research) * validation_fraction)
    split = DatasetSplit(
        total_bars=len(rows),
        discovery_bars=len(research) - validation_bars,
        validation_bars=validation_bars,
        research_bars=len(research),
        holdout_bars=len(holdout),
        holdout_start=holdout[0].time.isoformat() if holdout else "",
        holdout_end=holdout[-1].time.isoformat() if holdout else "",
        holdout_sha256=dataset_digest(holdout) if holdout else "",
        sealed=bool(holdout),
        holdout_fraction=holdout_fraction,
    )
    return research, split, holdout


def holdout_state(path: str | Path = DEFAULT_CONFIRMATION_PATH) -> dict[str, Any]:
    """Existing holdout-confirmation records (empty structure when nothing was run)."""
    target = Path(path)
    if not target.exists():
        return {"confirmations": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"confirmations": []}
    if not isinstance(payload, dict):
        return {"confirmations": []}
    payload.setdefault("confirmations", [])
    return payload


def record_holdout_confirmation(
    *,
    family_id: str,
    dataset_sha256: str,
    holdout_sha256: str,
    survivors: list[str],
    results: dict[str, Any] | None = None,
    path: str | Path = DEFAULT_CONFIRMATION_PATH,
    force: bool = False,
) -> dict[str, Any]:
    """Record the single confirmation run on the sealed holdout.

    The holdout is the one piece of data selection never touched, so looking at it is a significant
    act and gets logged as one. A second confirmation for the same family and dataset is refused
    unless ``force`` is set: re-running until the holdout agrees is how a holdout stops being one.
    """
    target = Path(path)
    payload = holdout_state(target)
    existing = [
        row
        for row in payload.get("confirmations", [])
        if row.get("family_id") == family_id and row.get("dataset_sha256") == dataset_sha256
    ]
    if existing and not force:
        raise RuntimeError(
            "holdout already confirmed for this family and dataset; refusing to overwrite "
            "(pass force=True only if the previous record is known to be void)"
        )
    record = {
        "confirmed_at": datetime.now(UTC).isoformat(),
        "family_id": family_id,
        "dataset_sha256": dataset_sha256,
        "holdout_sha256": holdout_sha256,
        "survivors": list(survivors),
        "results": dict(results or {}),
        "interpretation": (
            "a confirmation run is evidence about the research process; it is not another dataset "
            "to optimise against"
        ),
    }
    payload["confirmations"] = [
        row
        for row in payload.get("confirmations", [])
        if not (row.get("family_id") == family_id and row.get("dataset_sha256") == dataset_sha256)
    ]
    payload["confirmations"].append(record)
    payload["updated_at"] = record["confirmed_at"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return record


@dataclass
class ResearchManifest:
    """Everything needed to reproduce (or audit) one research run."""

    family_id: str = ""
    hypothesis_version: str = "1"
    dataset_path: str = ""
    dataset_sha256: str = ""
    research_sha256: str = ""
    total_bars: int = 0
    research_bars: int = 0
    date_range: tuple[str, str] = ("", "")
    symbol: str = ""
    timeframe: str = ""
    spread_price: float = 0.0
    slippage_price: float = 0.0
    commission_per_lot: float = 0.0
    starting_balance: float = 0.0
    risk_pct: float = 0.0
    max_volume: float = 0.0
    max_exposure_pct: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    correction: dict[str, Any] = field(default_factory=dict)
    holdout: dict[str, Any] = field(default_factory=dict)
    code_commit: str = ""
    python: str = ""
    platform: str = ""
    built_at: str = ""
    report_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["built_at"] = self.built_at or datetime.now(UTC).isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResearchManifest:
        known = {f for f in cls.__dataclass_fields__}
        data = {key: value for key, value in payload.items() if key in known}
        if isinstance(data.get("date_range"), list):
            data["date_range"] = tuple(data["date_range"])
        return cls(**data)

    def to_run_arguments(self) -> dict[str, Any]:
        """Arguments for a reproducible re-run of the same experiment."""
        return {
            "family_id": self.family_id,
            "hypothesis_version": self.hypothesis_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "spread_price": self.spread_price,
            "slippage_price": self.slippage_price,
            "commission_per_lot": self.commission_per_lot,
            "starting_balance": self.starting_balance,
            "risk_pct": self.risk_pct,
            "max_volume": self.max_volume,
            "max_exposure_pct": self.max_exposure_pct,
            "alpha": self.correction.get("alpha"),
            "method": self.correction.get("primary"),
            "permutations": self.statistics.get("permutations"),
            "seed": self.statistics.get("seed"),
            "holdout_fraction": self.holdout.get("holdout_fraction"),
            "dataset_sha256": self.dataset_sha256,
        }


def build_manifest(
    *,
    dataset_path: str | Path,
    all_bars: Any,
    research_bars: Any,
    symbol: str,
    timeframe: str,
    costs: dict[str, Any],
    backtest: dict[str, Any],
    family: dict[str, Any],
    statistics: dict[str, Any],
    correction: dict[str, Any],
    holdout: dict[str, Any],
    directory: str | Path = ".",
) -> ResearchManifest:
    rows = list(all_bars)
    return ResearchManifest(
        family_id=str(family.get("family_id", "")),
        hypothesis_version=str(family.get("hypothesis_version", "1")),
        dataset_path=str(dataset_path),
        dataset_sha256=dataset_digest(rows),
        research_sha256=dataset_digest(research_bars),
        total_bars=len(rows),
        research_bars=len(list(research_bars)),
        date_range=(
            rows[0].time.isoformat() if rows else "",
            rows[-1].time.isoformat() if rows else "",
        ),
        symbol=symbol,
        timeframe=timeframe,
        spread_price=float(costs.get("spread_price", 0.0)),
        slippage_price=float(costs.get("slippage_price", 0.0)),
        commission_per_lot=float(costs.get("commission_per_lot", 0.0)),
        starting_balance=float(backtest.get("starting_balance", 0.0)),
        risk_pct=float(backtest.get("risk_pct", 0.0)),
        max_volume=float(backtest.get("max_volume", 0.0)),
        max_exposure_pct=float(backtest.get("max_exposure_pct", 0.0)),
        candidates=list(family.get("candidates", [])),
        statistics=dict(statistics),
        correction=dict(correction),
        holdout=dict(holdout),
        code_commit=code_commit(directory),
        python=sys.version.split()[0],
        platform=platform.platform(),
        built_at=datetime.now(UTC).isoformat(),
    )


def write_manifest(manifest: ResearchManifest, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return target


def read_manifest(path: str | Path) -> ResearchManifest:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"manifest not found: {target}")
    return ResearchManifest.from_dict(json.loads(target.read_text(encoding="utf-8")))


def verify_manifest_dataset(manifest: ResearchManifest, bars: Any) -> tuple[bool, str]:
    """Check the data still matches the manifest before reproducing a run on it."""
    observed = dataset_digest(bars)
    if not manifest.dataset_sha256:
        return False, "manifest has no dataset hash"
    if observed != manifest.dataset_sha256:
        return False, (
            f"dataset hash mismatch: manifest={manifest.dataset_sha256[:12]} now={observed[:12]}"
        )
    return True, "dataset hash matches the manifest"


__all__ = [
    "DEFAULT_CONFIRMATION_PATH",
    "HOLDOUT_FRACTION",
    "DatasetSplit",
    "HypothesisRecord",
    "ResearchManifest",
    "build_manifest",
    "code_commit",
    "dataset_digest",
    "family_id_for",
    "holdout_state",
    "read_manifest",
    "record_holdout_confirmation",
    "slug",
    "split_dataset",
    "verify_manifest_dataset",
    "write_manifest",
]

