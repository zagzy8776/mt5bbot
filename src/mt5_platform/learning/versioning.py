"""Configuration versioning (Phase E).

Tracks configuration changes with full provenance. No automatic mutation.

A validated lesson can PROPOSE a configuration change, but it must be
explicitly approved before it takes effect.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.common.enums import ConfigurationChangeType
from mt5_platform.learning.models import ConfigurationVersion


class ConfigurationVersionStore:
    """Immutable configuration version history."""

    def __init__(self) -> None:
        self._versions: dict[str, ConfigurationVersion] = {}
        self._current_version: str = "v1.0.0"

    def propose_change(
        self,
        change_type: ConfigurationChangeType,
        reason: str,
        configuration: dict[str, Any],
        *,
        approved_lesson_id: str | None = None,
        validation_evidence: list[str] | None = None,
    ) -> ConfigurationVersion:
        version = ConfigurationVersion(
            previous_version=self._current_version,
            new_version=self._bump_version(),
            change_type=change_type,
            reason=reason,
            approved_lesson_id=approved_lesson_id,
            validation_evidence=validation_evidence or [],
            configuration=configuration,
        )
        self._versions[version.version_id] = version
        return version

    def approve(self, version_id: str) -> None:
        version = self._versions.get(version_id)
        if version is None:
            raise KeyError(f"unknown version: {version_id}")
        self._current_version = version.new_version

    def get_current_version(self) -> str:
        return self._current_version

    def get_version(self, version_id: str) -> ConfigurationVersion | None:
        return self._versions.get(version_id)

    def list_versions(self) -> list[ConfigurationVersion]:
        return list(self._versions.values())

    def _bump_version(self) -> str:
        parts = self._current_version.lstrip("v").split(".")
        try:
            patch = int(parts[2]) + 1
            return f"v{parts[0]}.{parts[1]}.{patch}"
        except (IndexError, ValueError):
            return f"{self._current_version}.1"


__all__ = ["ConfigurationVersionStore"]
