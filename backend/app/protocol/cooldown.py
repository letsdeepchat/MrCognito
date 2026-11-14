"""24-hour consolidation state machine.

    READY --start--> ACTIVE --(budget elapsed | protocol finished)--> CONSOLIDATING --(24h)--> READY

* The budget clock is server wall-clock from ``started_at``; it cannot be paused.
* A session is force-completed the moment ``now >= started_at + budget``,
  and the lock is anchored at that hard deadline (not at the next request),
  so walking away does not shorten or extend the lock.
* Changing the daily budget never unlocks an active cooldown.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from ..clock import as_utc
from ..config import get_settings
from ..db import CognitiveProfile, TrainingSession


class Gate(str, Enum):
    READY = "READY"
    ACTIVE = "ACTIVE"
    CONSOLIDATING = "CONSOLIDATING"


class GateError(Exception):
    def __init__(self, code: str, message: str, unlock_at: datetime | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.unlock_at = code, message, unlock_at


def hard_deadline(session: TrainingSession) -> datetime:
    return as_utc(session.started_at) + timedelta(minutes=session.budget_minutes)


def gate_state(profile: CognitiveProfile, active: TrainingSession | None, now: datetime) -> Gate:
    if active is not None and active.state == "ACTIVE" and now < hard_deadline(active):
        return Gate.ACTIVE
    unlock = as_utc(profile.next_unlock_at)
    if unlock is not None and now < unlock:
        return Gate.CONSOLIDATING
    return Gate.READY


def assert_can_start(profile: CognitiveProfile, active: TrainingSession | None, now: datetime) -> None:
    g = gate_state(profile, active, now)
    if g is Gate.ACTIVE:
        raise GateError("session_active", "A session is already in progress.")
    if g is Gate.CONSOLIDATING:
        raise GateError("consolidating", "Today's workout is done. Training unlocks after the 24-hour consolidation window.",
                        as_utc(profile.next_unlock_at))


def validate_budget(minutes: int) -> None:
    if minutes not in get_settings().allowed_budgets:
        raise GateError("invalid_budget", f"Daily budget must be one of {list(get_settings().allowed_budgets)} minutes.")


def complete(profile: CognitiveProfile, session: TrainingSession, now: datetime, reason: str) -> None:
    """Idempotently close a session and start the 24h lock."""
    if session.state != "ACTIVE":
        return
    ended = min(now, hard_deadline(session))
    session.state = "COMPLETED"
    session.end_reason = reason
    session.ended_at = ended
    profile.last_session_completed_at = ended
    profile.next_unlock_at = ended + timedelta(hours=get_settings().cooldown_hours)


def expired(session: TrainingSession, now: datetime) -> bool:
    return session.state == "ACTIVE" and now >= hard_deadline(session)
