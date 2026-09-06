"""Network resource optimization policy (M3) — classify, do not blindly block."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any


class ResourceClass(StrEnum):
    ESSENTIAL = "essential"
    OPTIONAL = "optional"
    NON_ESSENTIAL = "non_essential"
    UNKNOWN = "unknown"


# Defaults are conservative. Calibrate per dashboard before blocking.
DEFAULT_ABORT_TYPES = frozenset({"image", "media", "font"})
DEFAULT_ALLOW_TYPES = frozenset({"document", "xhr", "fetch", "script", "websocket"})


def classify_resource(resource_type: str, url: str) -> ResourceClass:
    """Classify a resource. UNKNOWN must not be aborted without dashboard validation."""
    rtype = resource_type.lower().strip()
    lower_url = url.lower()

    if rtype in DEFAULT_ALLOW_TYPES:
        return ResourceClass.ESSENTIAL

    if any(token in lower_url for token in ("analytics", "tracking", "beacon", "pixel")):
        return ResourceClass.NON_ESSENTIAL

    if rtype in DEFAULT_ABORT_TYPES:
        return ResourceClass.NON_ESSENTIAL

    return ResourceClass.UNKNOWN


def should_abort(resource_type: str, url: str, *, calibrated: bool = False) -> bool:
    """Only abort NON_ESSENTIAL when the target dashboard has been calibrated."""
    if not calibrated:
        return False
    return classify_resource(resource_type, url) is ResourceClass.NON_ESSENTIAL


async def attach_resource_filter(
    context: Any,
    *,
    calibrated: bool = False,
    on_decision: Callable[[str, str, bool], Awaitable[None] | None] | None = None,
) -> None:
    """Attach Playwright route interception when a real context supports ``route``."""

    route_fn = getattr(context, "route", None)
    if route_fn is None:
        return

    async def _handler(route: Any) -> None:
        request = route.request
        resource_type = getattr(request, "resource_type", "") or ""
        url = getattr(request, "url", "") or ""
        abort = should_abort(resource_type, url, calibrated=calibrated)
        if on_decision is not None:
            maybe = on_decision(resource_type, url, abort)
            if maybe is not None and hasattr(maybe, "__await__"):
                await maybe
        if abort:
            await route.abort()
        else:
            await route.continue_()

    await route_fn("**/*", _handler)
