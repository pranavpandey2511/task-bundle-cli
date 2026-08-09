"""Sortable command identifiers."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_command_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    prefix = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{secrets.token_hex(4)}"
