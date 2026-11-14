"""Daily sessions lock exactly at the 10/20/30-minute threshold and stay locked for 24h."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import CognitiveProfile, TrainingSession
from app.protocol import cooldown
from app.protocol.phases import ORDER, phase_seconds

from .conftest import act, new_user, start


def _profile(**kw):
    p = CognitiveProfile(user_id="u", daily_budget_minutes=20)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


T0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("budget", [10, 20, 30])
def test_phase_budgets_sum_to_daily_budget(budget):
    assert sum(phase_seconds(budget, p) for p in ORDER) == budget * 60


def test_state_machine_transitions():
    p = _profile()
    assert cooldown.gate_state(p, None, T0) is cooldown.Gate.READY
    s = TrainingSession(user_id="u", budget_minutes=10, state="ACTIVE", started_at=T0)
    assert cooldown.gate_state(p, s, T0 + timedelta(minutes=9, seconds=59)) is cooldown.Gate.ACTIVE
    assert cooldown.expired(s, T0 + timedelta(minutes=10))
    cooldown.complete(p, s, T0 + timedelta(hours=5), "budget_reached")  # user came back hours later
    assert s.ended_at == T0 + timedelta(minutes=10), "lock anchors at the hard deadline"
    assert p.next_unlock_at == T0 + timedelta(minutes=10, hours=24)
    assert cooldown.gate_state(p, s, T0 + timedelta(hours=24)) is cooldown.Gate.CONSOLIDATING
    assert cooldown.gate_state(p, s, T0 + timedelta(minutes=10, hours=24)) is cooldown.Gate.READY


def test_complete_is_idempotent():
    p = _profile()
    s = TrainingSession(user_id="u", budget_minutes=10, state="ACTIVE", started_at=T0)
    cooldown.complete(p, s, T0 + timedelta(minutes=3), "protocol_complete")
    first = p.next_unlock_at
    cooldown.complete(p, s, T0 + timedelta(minutes=9), "budget_reached")
    assert p.next_unlock_at == first and s.end_reason == "protocol_complete"


def test_invalid_budget_rejected():
    for bad in (0, 5, 15, 45, 60):
        with pytest.raises(cooldown.GateError):
            cooldown.validate_budget(bad)


@pytest.mark.parametrize("budget", [10, 20, 30])
async def test_api_locks_at_threshold(client, fake_clock, budget):
    h = await new_user(client, budget)
    s = await start(client, h)
    sid, t0 = s["session_id"], fake_clock.t

    fake_clock.t = t0 + timedelta(minutes=budget, seconds=-1)
    me = (await client.get("/api/me", headers=h)).json()
    assert me["gate"] == "ACTIVE"

    fake_clock.t = t0 + timedelta(minutes=budget)
    me = (await client.get("/api/me", headers=h)).json()
    assert me["gate"] == "CONSOLIDATING"
    assert datetime.fromisoformat(me["next_unlock_at"]) == t0 + timedelta(minutes=budget, hours=24)

    # actions on the old session are refused
    r = await client.post(f"/api/sessions/{sid}/action", headers=h, json={"action": {"type": "advance_phase"}})
    assert r.status_code == 423
    sess = (await client.get(f"/api/sessions/{sid}", headers=h)).json()
    assert sess["state"] == "COMPLETED" and sess["end_reason"] == "budget_reached"

    # a new session is refused until exactly 24h after the threshold
    r = await client.post("/api/sessions", headers=h)
    assert r.status_code == 423 and r.json()["detail"]["code"] == "consolidating"
    fake_clock.t = t0 + timedelta(minutes=budget, hours=24, seconds=-1)
    assert (await client.post("/api/sessions", headers=h)).status_code == 423
    fake_clock.t = t0 + timedelta(minutes=budget, hours=24)
    assert (await client.post("/api/sessions", headers=h)).status_code == 200


async def test_abandoned_session_locks_from_deadline_not_return_time(client, fake_clock):
    h = await new_user(client, 10)
    s = await start(client, h)
    t0 = fake_clock.t
    fake_clock.advance(hours=7)  # user walked away
    me = (await client.get("/api/me", headers=h)).json()
    assert me["gate"] == "CONSOLIDATING"
    assert datetime.fromisoformat(me["next_unlock_at"]) == t0 + timedelta(minutes=10, hours=24)


async def test_cannot_start_two_sessions(client):
    h = await new_user(client, 10)
    await start(client, h)
    r = await client.post("/api/sessions", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "session_active"


async def test_budget_change_never_unlocks(client, fake_clock):
    h = await new_user(client, 10)
    await start(client, h)
    r = await client.put("/api/me/settings", json={"daily_budget_minutes": 30}, headers=h)
    assert r.status_code == 409  # cannot change mid-session
    fake_clock.advance(minutes=10)
    r = await client.put("/api/me/settings", json={"daily_budget_minutes": 30}, headers=h)
    assert r.status_code == 200 and r.json()["gate"] == "CONSOLIDATING"
    assert (await client.post("/api/sessions", headers=h)).status_code == 423


async def test_early_protocol_completion_also_locks(client, fake_clock):
    h = await new_user(client, 10)
    s = await start(client, h)
    sid = s["session_id"]
    for _ in range(4):
        r = await act(client, h, sid, {"type": "advance_phase"})
    assert r["session"]["phase"] == "RECALL_CONSOLIDATION"
    await act(client, h, sid, {"type": "recall_reconstruct", "placement": "8/8/8/8/8/8/8/8"})
    r = await act(client, h, sid, {"type": "recall_reflect", "habit_tag": "blunder-check before moving",
                                   "key_moment": "I missed the pin on the long diagonal entirely."})
    assert r["session"]["state"] == "COMPLETED" and r["session"]["end_reason"] == "protocol_complete"
    me = (await client.get("/api/me", headers=h)).json()
    assert me["gate"] == "CONSOLIDATING" and me["habit_for_tomorrow"] == "blunder-check before moving"
    assert datetime.fromisoformat(me["next_unlock_at"]) == fake_clock.t + timedelta(hours=24)


async def test_phases_advance_on_schedule(client, fake_clock):
    h = await new_user(client, 20)
    s = await start(client, h)
    sid, t0 = s["session_id"], fake_clock.t
    expected = t0
    for phase in ORDER[:-1]:
        expected += timedelta(seconds=phase_seconds(20, phase))
        fake_clock.t = expected
        sess = (await client.get(f"/api/sessions/{sid}", headers=h)).json()
        assert sess["phase"] != phase.value
        assert datetime.fromisoformat(sess["phase_started_at"]) == expected


async def test_wellness_ack_required_before_first_session(client):
    h = await new_user(client, 10, ack=False)
    r = await client.post("/api/sessions", headers=h)
    assert r.status_code == 428 and "does not diagnose" in r.json()["detail"]["notice"]
