"""Correlation and internal execution IDs."""

from __future__ import annotations

import uuid


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def new_execution_id() -> str:
    """Unique internal ID for every execution attempt."""
    return f"exec_{uuid.uuid4().hex}"


def new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex}"


def new_signal_id() -> str:
    return f"sig_{uuid.uuid4().hex}"
