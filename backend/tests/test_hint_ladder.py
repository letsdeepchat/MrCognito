"""Hint escalation strictly increments one tier at a time."""
from __future__ import annotations

import chess
import pytest

from app.coach.hints import HintDenied, HintLadder

from .conftest import act, new_user, start


# ---------- pure state machine ----------------------------------------------
def test_requires_attempt_before_each_tier():
    lad = HintLadder()
    with pytest.raises(HintDenied) as e:
        lad.advance()
    assert e.value.code == "attempt_required"
    lad.record_attempt()
    assert lad.advance() == 1
    with pytest.raises(HintDenied):
        lad.advance()  # no new attempt since tier 1
    lad.record_attempt()
    assert lad.advance() == 2


def test_never_skips_a_tier():
    lad = HintLadder()
    for _ in range(5):
        lad.record_attempt()
    for requested in (2, 3, 4):
        with pytest.raises(HintDenied) as e:
            lad.advance(requested_tier=requested)
        assert e.value.code == "tier_skip"
    assert lad.advance(requested_tier=1) == 1


def test_multiple_attempts_still_buy_only_one_tier():
    lad = HintLadder()
    for _ in range(3):
        lad.record_attempt()
    assert lad.advance() == 1
    with pytest.raises(HintDenied):
        lad.advance()


def test_tier4_needs_three_failures_or_forfeit():
    lad = HintLadder(tier=3, attempts=2, attempts_at_last_hint=1)
    with pytest.raises(HintDenied) as e:
        lad.advance()
    assert e.value.code == "post_mortem_locked"
    assert lad.advance(forfeit=True) == 4

    lad = HintLadder(tier=3, attempts=3, attempts_at_last_hint=2)
    assert lad.advance() == 4
    with pytest.raises(HintDenied) as e:
        lad.advance()
    assert e.value.code == "ladder_exhausted"


@pytest.mark.parametrize("seq", [[1, 1, 1, 1], [3, 1, 2, 1]])
def test_monotone_plus_one_property(seq):
    """Whatever the attempt pattern, observed tiers form 1,2,3,... with no gaps."""
    lad, seen = HintLadder(), []
    for n_attempts in seq:
        for _ in range(n_attempts):
            lad.record_attempt()
        try:
            seen.append(lad.advance())
        except HintDenied:
            pass
    assert seen == list(range(1, len(seen) + 1))


# ---------- through the API (phase 2 puzzles) -------------------------------
async def _to_phase2(client, h):
    s = await start(client, h)
    sid = s["session_id"]
    r = await act(client, h, sid, {"type": "advance_phase"})
    assert r["session"]["phase"] == "PATTERN_INHIBITION"
    return sid, r["session"]


async def _wrong_move(engine, view) -> str:
    """The engine's worst legal move: certainly not an accepted solution."""
    b = chess.Board(view["board"]["fen"])
    an = await engine.analyse(b, depth=8, multipv=b.legal_moves.count())
    return an.lines[-1].move.uci()


async def test_api_hint_ladder_one_tier_per_attempt(client, engine):
    h = await new_user(client, 20)
    sid, s = await _to_phase2(client, h)
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "attempt_required"

    # wrong diagnosis is an attempt
    await act(client, h, sid, {"type": "pattern_diagnose", "motif": "interference", "tempting_move": "", "why": "?"})
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={"requested_tier": 2})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tier_skip"
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={"requested_tier": 1})
    assert r.status_code == 200 and r.json()["hint"]["tier"] == 1
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={})
    assert r.status_code == 409  # no attempt since last hint

    sess = (await client.get(f"/api/sessions/{sid}", headers=h)).json()
    tiers = [1]
    for _ in range(2):
        v = sess["view"]
        assert v["step"] == "move"
        res = await act(client, h, sid, {"type": "pattern_move", "uci": await _wrong_move(engine, v)})
        assert not res["result"].get("item_complete")
        r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={})
        assert r.status_code == 200, r.text
        tiers.append(r.json()["hint"]["tier"])
        sess = r.json()["session"]
    assert tiers == [1, 2, 3]
    assert len(r.json()["hint"]["candidates"]) == 2
    # tier 4 is locked: only 3 failed attempts so far would open it, but no new attempt since tier 3
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "attempt_required"
    # third failed move exhausts the move budget -> guided post-mortem (tier 4), user inputs each move
    res = await act(client, h, sid, {"type": "pattern_move", "uci": await _wrong_move(engine, r.json() and sess["view"])})
    assert res["result"]["move_budget_exhausted"] and res["session"]["view"]["hint_tier"] == 4


async def test_api_forfeit_opens_post_mortem_only_from_tier3(client):
    h = await new_user(client, 20)
    sid, s = await _to_phase2(client, h)
    await act(client, h, sid, {"type": "pattern_diagnose", "motif": "interference", "tempting_move": "", "why": ""})
    r = await client.post(f"/api/sessions/{sid}/hint", headers=h, json={"forfeit": True, "requested_tier": 4})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tier_skip"
