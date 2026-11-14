"""Guardrails, integrity monitor, masking, plan parsing, metrics and content sanity."""
from __future__ import annotations

import chess
import pytest

from app import integrity, telemetry as tm
from app.coach.guardrails import OutputPolicy, check_output, detect_offloading, tier_policy
from app.coach.plan_parser import normalise_spoken, parse_plan
from app.coach.rag import MemoryStore
from app.protocol.common import masked_board, sector_phrase
from app.protocol.content import validate_content
from app.protocol.p5_recall import placement_accuracy

from .conftest import act, new_user, start


@pytest.mark.parametrize("text", ["What is the best move?", "am I winning", "Give me the solution", "just tell me",
                                  "what should I play here", "what's the eval", "which move wins?", "show me the engine line"])
def test_offloading_detected(text):
    assert detect_offloading(text)


@pytest.mark.parametrize("text", ["I think the knight is overloaded", "Is my king safe after castling?",
                                  "I want to attack the kingside"])
def test_genuine_questions_not_refused(text):
    assert not detect_offloading(text)


async def test_api_refuses_offloading_and_redirects(client):
    h = await new_user(client, 10)
    s = await start(client, h)
    sid = s["session_id"]
    await act(client, h, sid, {"type": "advance_phase"})
    for msg in ("What is the best move?", "Am I winning?", "Give me the solution"):
        r = (await client.post(f"/api/sessions/{sid}/coach", headers=h, json={"message": msg})).json()
        assert r["refused"] and r["question"].endswith("?")


def test_output_policy():
    p = OutputPolicy(tier=0, allowed_moves={"Nf3"}, forbidden_moves={"Bb5", "e5"})
    assert check_output("Your Nf3 eyes the centre. What about the e-file?", p) == []
    assert check_output("Consider Bb5 instead.", p)
    assert check_output("Push to e5!", p)  # pawn push spelled as a square
    assert check_output("You're up +1.4", p)
    assert check_output("Roughly 70% to win", p)
    assert check_output("There's mate in 3", p)
    assert check_output("Play g1f3", p)
    t1 = tier_policy(1, set(), set())
    assert check_output("Look at the knight on f3", t1)
    assert check_output("Compare how active each side's worst unit is.", t1) == []


def _samples(times, top1):
    return [[t, k] for t, k in zip(times, top1)]


def test_integrity_monitor():
    uniform = _samples([10000, 10400, 9800, 10100, 10200, 9900, 10050, 10300], [1] * 8)
    bursty = _samples([2000, 45000, 6000, 30000, 3000, 60000, 8000, 15000], [1] * 8)
    human = _samples([10000] * 8, [1, 0, 0, 1, 0, 1, 0, 0])
    assert integrity.evaluate(uniform).suspicious
    assert not integrity.evaluate(bursty).suspicious
    assert not integrity.evaluate(human).suspicious
    assert not integrity.evaluate(uniform[:5]).suspicious


def test_masking_hides_information_server_side():
    fen = chess.STARTING_FEN
    m0, m1, m2, m3 = (masked_board(fen, i) for i in range(4))
    assert m0["fen"] == fen and m0["pieces"]["e1"] == "K"
    assert "fen" not in m1 and m1["pieces"]["e8"] == "K"  # silhouette: type only, colour hidden
    assert set(m2["pieces"].values()) == {"*"} and len(m2["pieces"]) == 32
    assert m3["pieces"] == {} and "fen" not in m3


def test_sector_phrase_localises_without_naming_move():
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    phrase, sec = sector_phrase(b, b.parse_san("Bxf7+"))
    assert "diagonal" in phrase and "Bxf7" not in phrase


def test_spoken_plan_parsing():
    assert "Nf3" in normalise_spoken("knight f three")
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    p = parse_plan(fen, "candidates bishop b5 and bishop c4. Opponent replies knight f6. "
                        "Purpose is to pressure the knight defending e5")
    assert p["candidates"] == ["Bb5", "Bc4"] and p["opponent_reply"] == "Nf6" and p["complete"]


def test_recall_accuracy():
    assert placement_accuracy(chess.STARTING_FEN, chess.STARTING_FEN.split()[0]) == 1.0
    assert placement_accuracy(chess.STARTING_FEN, "8/8/8/8/8/8/8/8") == 0.0
    half = "rnbqkbnr/pppppppp/8/8/8/8/8/8"
    assert 0.6 < placement_accuracy(chess.STARTING_FEN, half) < 0.7


def test_staircase_targets_72_percent():
    r = 1200.0
    assert tm.staircase(r, 1.0) > r > tm.staircase(r, 0.0)
    assert abs(tm.staircase(r, tm.TARGET_SUCCESS) - r) < 1e-9


def test_rma_rewards_consistent_plans():
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    mv = b.parse_san("Bc4")
    good = tm.reasoning_alignment(b, mv, [mv, b.parse_san("Bb5")], "Nf6", None,
                                  "develop and activate the bishop toward the king")
    bad = tm.reasoning_alignment(b, mv, [b.parse_san("d4"), b.parse_san("Bb5")], "", None, "trade queens")
    assert good > bad


def test_fatigue_detector():
    fresh = [[i * 30, 8000, 1] for i in range(6)]
    tired = [[0, 6000, 1], [30, 6500, 1], [60, 7000, 1], [90, 15000, 0], [120, 16000, 0], [150, 18000, 0]]
    assert not tm.fatigue(fresh).fatigued
    assert tm.fatigue(tired).fatigued


def test_content_bank_is_legal():
    assert validate_content() == []


async def test_rag_retrieves_relevant_principles():
    store = MemoryStore()
    hits = await store.search("back_rank rook_endgame", k=3, tags=["back_rank", "rook_endgame"])
    assert any(h.id == "back-rank" for h in hits)
    for c in store.chunks:  # knowledge base must be move-free
        assert check_output(c.text, OutputPolicy()) == [], c.id
