"""Context fingerprint for isolated BrowserContexts (M1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextConfig:
    """Per-context browser settings. Not intended to defeat access controls."""

    user_agent: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 720
    locale: str = "en-US"
    timezone_id: str = "UTC"
    device_scale_factor: float = 1.0
    ignore_https_errors: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_playwright_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "device_scale_factor": self.device_scale_factor,
            "ignore_https_errors": self.ignore_https_errors,
        }
        if self.user_agent:
            options["user_agent"] = self.user_agent
        options.update(self.extra)
        return options
