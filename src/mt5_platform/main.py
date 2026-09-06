"""CLI / process entrypoint."""

from __future__ import annotations

import uvicorn

from mt5_platform.api import create_app
from mt5_platform.config import get_settings


def run() -> None:
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
