"""Test isolation from the live deployment configuration.

`.env` is a *runtime* file: it points `RISK_STATE_PATH` at the real `risk_state.json`, applies
the deployment's own risk sizing (`MAX_POSITION_SIZE=0.02`), enables the mt5 backend and sets an
API token. Inheriting that made a plain `pytest` run fail for unrelated reasons and — worse —
*persisted* kill-switch engagements raised by tests into the live risk state file.

So the suite is made blind to `.env`: it runs against the documented defaults, which is what the
tests assert. Nothing here touches `.env`, `risk_state.json`, or the running bot's kill switch.
"""

from __future__ import annotations

import os

from mt5_platform.config import Settings

# Applies to every Settings() constructed from now on: tests must not read the deployment file.
Settings.model_config["env_file"] = None

# Belt and braces for anything exported into the developer's shell: these are the values the
# suite's expectations depend on, and none of them may come from a live runtime.
_HERMETIC_ENV: dict[str, str] = {
    "RISK_STATE_PATH": "",  # never read or write the live kill-switch / pause state
    "API_TOKEN": "",  # the loopback API is exercised unauthenticated
    "EXECUTION_BACKEND": "mock",  # tests needing mt5 pass execution_backend="mt5" explicitly
    "STORAGE_BACKEND": "memory",
    "EMERGENCY_KILL_SWITCH": "false",
    "DEFAULT_SYMBOL": "XAUUSD",
}

for _key, _value in _HERMETIC_ENV.items():
    os.environ[_key] = _value

