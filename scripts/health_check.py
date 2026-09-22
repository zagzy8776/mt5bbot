"""Server health check — verifies all critical services are alive.

Run periodically via Task Scheduler or external monitor.
Exit code 0 = healthy, 1 = degraded, 2 = critical.

Checks:
  - API /health endpoint responds 200
  - MT5 terminal process is running
  - Caddy process is running
  - Disk free space > 1 GB
  - PostgreSQL connectivity
"""

import json
import subprocess
import sys
import urllib.request


def check_api() -> tuple[bool, str]:
    """Check FastAPI /health endpoint."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/health",
            headers={"Authorization": "Bearer healthcheck"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            if resp.status == 200 and body.get("status") == "up":
                return True, "API healthy"
            return False, f"API unhealthy: {body.get('status')}"
    except Exception as exc:
        return False, f"API unreachable: {exc}"


def check_process(name: str) -> tuple[bool, str]:
    """Check if a Windows process is running."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-Command",
                f"Get-Process {name} -ErrorAction SilentlyContinue | "
                "Measure-Object | Select-Object -ExpandProperty Count",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        count = int(result.stdout.strip() or "0")
        if count > 0:
            return True, f"{name} running ({count} processes)"
        return False, f"{name} NOT running"
    except Exception as exc:
        return False, f"{name} check failed: {exc}"


def check_disk(min_gb: float = 1.0) -> tuple[bool, str]:
    """Check disk free space."""
    try:
        import shutil
        usage = shutil.disk_usage("C:\\")
        free_gb = usage.free / (1024**3)
        if free_gb >= min_gb:
            return True, f"Disk OK: {free_gb:.1f} GB free"
        return False, f"Disk LOW: {free_gb:.1f} GB free (need {min_gb} GB)"
    except Exception as exc:
        return False, f"Disk check failed: {exc}"


def check_postgres() -> tuple[bool, str]:
    """Check PostgreSQL connectivity via SQLAlchemy."""
    try:
        import asyncio
        import os

        from mt5_platform.storage.db import create_engine, normalize_database_url

        url = os.environ.get("DATABASE_URL", "")
        if not url:
            return False, "DATABASE_URL not set"

        engine = create_engine(normalize_database_url(url))

        async def _test() -> None:
            from sqlalchemy import text
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()

        asyncio.run(_test())
        return True, "PostgreSQL connected"
    except Exception as exc:
        return False, f"PostgreSQL failed: {exc}"


def main() -> int:
    checks = [
        ("API", check_api),
        ("MT5 Terminal", lambda: check_process("terminal64")),
        ("Caddy", lambda: check_process("caddy")),
        ("Disk", lambda: check_disk(1.0)),
        ("PostgreSQL", check_postgres),
    ]

    results = []
    all_ok = True
    any_critical = False

    for name, fn in checks:
        ok, detail = fn()
        status = "✅" if ok else "❌"
        results.append(f"  {status} {name}: {detail}")
        if not ok:
            all_ok = False
            if name in ("API", "MT5 Terminal"):
                any_critical = True

    print("=" * 50)
    print("MT5BBOT HEALTH CHECK")
    print("=" * 50)
    for r in results:
        print(r)
    print("=" * 50)

    if any_critical:
        print("RESULT: CRITICAL — key service down")
        return 2
    elif not all_ok:
        print("RESULT: DEGRADED — some checks failed")
        return 1
    else:
        print("RESULT: HEALTHY")
        return 0


if __name__ == "__main__":
    sys.exit(main())
