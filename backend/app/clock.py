"""Single source of server time so the 24h state machine is testable."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def utcnow() -> datetime:
    return _now()


def set_clock(fn: Callable[[], datetime]) -> None:
    global _now
    _now = fn


def reset_clock() -> None:
    set_clock(lambda: datetime.now(timezone.utc))


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalise everything to aware UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
