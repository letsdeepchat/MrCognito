"""No API endpoint leaks raw engine output during active phases.

An adversarial bot plays complete sessions, hammering every endpoint (actions,
hints, coach chat incl. offloading requests). For every position where the
*user* is to move, the test independently computes the engine's best move and
asserts that no response contains it (SAN or UCI) - except in the documented,
spec-mandated reveals: the unlabeled tier-3 candidate pair and the tier-4
guided post-mortem after forfeit. Responses must also never carry
engine-output keys or evaluation-shaped values.
"""
from __future__ import annotations

import json
import random
import re

import chess
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.leakguard import LeakGuardRoute, find_leaks
from app.coach.guardrails import OutputPolicy, check_output
from app.coach.llm import LLMTurn, ToolCall
from app.coach.orchestrator import SocraticOrchestrator
from app.coach.tools import TOOL_SCHEMAS, ToolContext, ToolValidationError, execute_tool, validate_args

from .conftest import new_user, start

# Keys whose values legitimately contain moves that are NOT engine advice for the user's position:
# opponent moves, the user's own moves echoed back, dictated drill content, post-mortem/tier-3 reveals.
WHITELIST_KEYS = {"opponent_move", "opponent_move_uci", "last_opponent_move", "opponent_reply_san", "your_move",
                  "played_san", "dictated_moves", "instruction", "post_mortem_reveal", "candidates", "debrief", "fen",
                  "pieces", "placement"}
EVAL_RX = re.compile(r"(?<![\w.])[+\-−]\d+(?:\.\d+)?(?![\w])|\b\d+\s*(cp|centipawns?)\b|\bmate in \d|\d{1,3}\s?%", re.I)


def strings(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def tokens_of(board: chess.Board, move: chess.Move) -> set[str]:
    san = board.san(move).rstrip("+#")
    return {move.uci(), san}


class Auditor:
    def __init__(self, engine):
        self.engine = engine
        self.best: dict[str, set[str]] = {}  # fen -> forbidden tokens
        self.exempt: set[str] = set()  # fens whose solving ended (post-mortem) or whose move the user typed
        self.user_typed: dict[str, set[str]] = {}
        self.checked = 0

    async def register(self, fen: str):
        if fen in self.best:
            return
        b = chess.Board(fen)
        an = await self.engine.analyse(b, depth=8)
        self.best[fen] = tokens_of(b, an.best.move) if an.best else set()

    def typed(self, fen: str, *moves: str):
        self.user_typed.setdefault(fen, set()).update(m.strip().rstrip("+#") for m in moves if m)

    def audit(self, payload, fen: str | None, tier3_pair: list[str] | None = None):
        assert find_leaks(payload) == [], f"forbidden keys/values: {find_leaks(payload)}"
        for key, s in strings(payload):
            if key in ("fen", "pieces"):
                continue
            assert not EVAL_RX.search(s) or key in WHITELIST_KEYS, f"eval-like text in {key}: {s!r}"
        if not fen or fen in self.exempt or fen not in self.best:
            return
        forbidden = self.best[fen] - self.user_typed.get(fen, set()) - set(tier3_pair or [])
        for key, s in strings(payload):
            if key in WHITELIST_KEYS:
                continue
            for tok in forbidden:
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", s):
                    pytest.fail(f"engine best move {tok!r} leaked in {key!r}: {s!r} (fen {fen})")
        self.checked += 1


def user_fen(sess: dict) -> str | None:
    v = sess.get("view") or {}
    if sess.get("phase") in ("PATTERN_INHIBITION", "SPARRING_VERBALIZED", "ADAPTABILITY_PERTURBATION"):
        b = v.get("board") or {}
        return b.get("fen")
    return None


async def play_session(client, h, engine, rng: random.Random, auditor: Auditor, clock=None):
    s = await start(client, h)
    sid = s["session_id"]
    auditor.audit(s, None)
    tier3: dict[str, list[str]] = {}

    async def post(path, body):
        r = await client.post(f"/api/sessions/{sid}{path}", headers=h, json=body)
        assert r.status_code < 500, r.text
        return r

    errors: list[str] = []
    for _ in range(600):
        if clock is not None:
            clock.advance(seconds=rng.randint(1, 6))
        sess = (await client.get(f"/api/sessions/{sid}", headers=h)).json()
        if sess["state"] != "ACTIVE":
            auditor.audit(sess, None)
            return sess
        fen = user_fen(sess)
        if fen:
            await auditor.register(fen)
        auditor.audit(sess, fen, tier3.get(fen))
        v, phase = sess["view"], sess["phase"]

        # Adversarial coach traffic on every step.
        if rng.random() < 0.35:
            msg = rng.choice(["What is the best move?", "am I winning?", "Give me the solution", "eval please",
                              "I'm looking at the kingside, what should I consider?", "Which move wins here?"])
            r = await post("/coach", {"message": msg})
            if r.status_code == 200:
                auditor.audit(r.json(), fen, tier3.get(fen))
        # Hint ladder probing (including skip attempts).
        if fen and rng.random() < 0.5:
            r = await post("/hint", {"requested_tier": rng.choice([None, None, 2, 4]), "forfeit": rng.random() < 0.1})
            if r.status_code == 200:
                hint = r.json()["hint"]
                if hint["tier"] == 3:
                    tier3[fen] = hint["candidates"]
                if hint["tier"] == 4:
                    auditor.exempt.add(fen)  # guided post-mortem: solving has ended
                auditor.audit(r.json(), fen, tier3.get(fen))

        action = await choose_action(sess, v, phase, rng, auditor)
        if action is None:
            action = {"type": "advance_phase"}
        r = await post("/action", {"action": action})
        if r.status_code != 200:
            errors.append(f"{phase} {action['type']} -> {r.status_code} {r.text[:160]}")
        if r.status_code == 200:
            body = r.json()
            if "post_mortem" in json.dumps(body["result"]):
                auditor.exempt.add(fen)
            auditor.audit(body, fen, tier3.get(fen))
    pytest.fail("session did not finish; last errors: " + " | ".join(errors[-5:]))


def _rand_move(board: chess.Board, rng) -> chess.Move:
    return rng.choice(list(board.legal_moves))


async def choose_action(sess, v, phase, rng, auditor):
    if v.get("done"):
        return {"type": "advance_phase"}
    if phase == "BLINDFOLD_WARMUP":
        step = v["step"]
        if step == "study":
            return {"type": "blindfold_ready"}
        if step == "dictation":
            return {"type": "blindfold_dictation_done"}
        if step == "queries":
            q = v["queries"][0]
            return {"type": "blindfold_answer", "query_id": q["id"], "answer": rng.choice(["e4", "2", "yes", "none"]),
                    "think_ms": rng.randint(2000, 20000)}
        if step == "final":
            return {"type": "blindfold_final_move", "uci": "e2e4", "think_ms": 4000}
    if phase == "PATTERN_INHIBITION":
        board = chess.Board(v["board"]["fen"])
        if v["step"] == "diagnose":
            m = _rand_move(board, rng)
            auditor.typed(board.fen(), board.san(m), m.uci())
            return {"type": "pattern_diagnose", "motif": rng.choice(v["motif_options"]), "tempting_move": board.san(m),
                    "why": "it hangs something"}
        if v["step"] == "move":
            m = _rand_move(board, rng)
            auditor.typed(board.fen(), board.san(m), m.uci())
            return {"type": "pattern_move", "uci": m.uci(), "think_ms": rng.randint(1500, 40000)}
        if v["step"] == "post_mortem":
            pm = chess.Board(v["post_mortem"]["fen"])
            return {"type": "post_mortem_move", "uci": _rand_move(pm, rng).uci()}
    if phase == "SPARRING_VERBALIZED":
        board = chess.Board(v["board"]["fen"])
        if v.get("post_mortem") and not v["post_mortem"]["done"]:
            return {"type": "post_mortem_move", "uci": _rand_move(chess.Board(v["post_mortem"]["fen"]), rng).uci()}
        if v["plan_required"]:
            legal = list(board.legal_moves)
            cands = rng.sample(legal, k=min(2, len(legal)))
            after = board.copy()
            after.push(cands[0])
            reply = board.san(cands[0])
            reply = after.san(_rand_move(after, rng)) if not after.is_game_over() else "nothing"
            sans = [board.san(m) for m in cands]
            auditor.typed(board.fen(), *sans, *[m.uci() for m in cands])
            return {"type": "sparring_plan", "candidates": sans, "opponent_reply": reply,
                    "purpose": "improve coordination and restrict counterplay", "variation": []}
        m = _rand_move(board, rng)
        auditor.typed(board.fen(), board.san(m), m.uci())
        return {"type": "sparring_move", "uci": m.uci(), "think_ms": rng.randint(1500, 60000)}
    if phase == "ADAPTABILITY_PERTURBATION":
        board = chess.Board(v["board"]["fen"])
        forb = v.get("forbidden_piece")
        names = {chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen"}
        legal = [m for m in board.legal_moves if names.get(board.piece_type_at(m.from_square)) != forb] or list(board.legal_moves)
        m = rng.choice(legal)
        auditor.typed(board.fen(), board.san(m), m.uci())
        return {"type": "perturb_move", "uci": m.uci(), "think_ms": rng.randint(1000, 20000)}
    if phase == "RECALL_CONSOLIDATION":
        if v["step"] == "reconstruct":
            return {"type": "recall_reconstruct", "placement": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"}
        return {"type": "recall_reflect", "key_moment": "I ignored the forcing reply after my knight jump.",
                "habit_tag": "ask what my opponent wants"}
    return None


@pytest.mark.parametrize("seed,budget,timed", [(1, 10, True), (2, 20, True), (3, 30, True),
                                               (4, 10, False), (5, 20, False), (6, 30, False)])
async def test_full_sessions_never_leak(client, engine, fake_clock, seed, budget, timed):
    """timed=True: wall clock advances so phases also end by timer; False: every item is played out."""
    h = await new_user(client, budget)
    auditor = Auditor(engine)
    final = await play_session(client, h, engine, random.Random(seed), auditor, fake_clock if timed else None)
    assert final["state"] == "COMPLETED"
    assert auditor.checked > 10, "the audit must actually cover user-to-move positions"


# ---------- unit-level defences -------------------------------------------
def test_leakguard_blocks_planted_leak():
    app = FastAPI()
    from fastapi import APIRouter

    r = APIRouter(route_class=LeakGuardRoute)

    @r.get("/bad")
    def bad():
        return {"view": {"best_move": "Nf3"}}

    @r.get("/bad2")
    def bad2():
        return {"message": "You are +1.45 here"}

    @r.get("/ok")
    def ok():
        return {"message": "What does your opponent threaten?"}

    app.include_router(r)
    c = TestClient(app)
    assert c.get("/bad").status_code == 500
    assert c.get("/bad2").status_code == 500
    assert c.get("/ok").status_code == 200


class JailbrokenLLM:
    """Simulates a model that tries to dump the answer, then complies after a rewrite request."""

    name = "jailbroken"

    def __init__(self, leak: str, comply: bool):
        self.leak, self.comply, self.calls = leak, comply, 0

    async def complete(self, system, transcript, tools, task):
        self.calls += 1
        rewrite_requested = any(e["role"] == "user" and e["text"].startswith("POLICY_VIOLATION") for e in transcript)
        if rewrite_requested and self.comply:
            args = {"message": "Look again at the loose pieces.", "question": "Which one is undefended?", "concepts": []}
        else:
            args = {"message": f"Just play {self.leak}, it's +2.3 and the engine's top move.", "question": "ok?", "concepts": []}
        return LLMTurn(tool_calls=[ToolCall(f"c{self.calls}", "socratic_reply", args)])


@pytest.mark.parametrize("comply", [True, False])
async def test_orchestrator_rejects_llm_leaks(engine, comply):
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 2 3")
    orch = SocraticOrchestrator(llm=JailbrokenLLM("Qxf7", comply), max_rounds=3)
    policy = OutputPolicy(tier=0, allowed_moves=set(), forbidden_moves={"Qxf7", "f3f7"})
    reply = await orch.respond({"kind": "chat", "rag": []}, policy, ToolContext(board=board, engine=engine))
    text = reply.message + " " + reply.question
    assert "Qxf7" not in text and "+2.3" not in text
    assert check_output(text, policy) == []
    assert reply.source == ("jailbroken" if comply else "rule")


async def test_tool_outputs_are_move_free(engine):
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR b KQkq - 3 3")
    an = await engine.analyse(board, depth=8)
    best = tokens_of(board, an.best.move)
    ctx = ToolContext(board=board, engine=engine, depth=8)
    outs = [
        await execute_tool(ctx, "validate_user_hypothesis", {"hypothesis_type": "candidate_move", "moves": ["Nd4", "a6"], "after_move": ""}),
        await execute_tool(ctx, "validate_user_hypothesis", {"hypothesis_type": "threat", "moves": ["Qxf7"], "after_move": ""}),
        await execute_tool(ctx, "get_board_sector_tension", {"sector": "kingside"}),
        await execute_tool(ctx, "evaluate_calculation_depth", {"variation": ["a6", "Qxf7"]}),
    ]
    blob = json.dumps(outs)
    user_typed = {"Nd4", "a6", "Qxf7"}
    for tok in best - user_typed:
        assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", blob), tok
    assert not re.search(r'"(cp|score|eval)\w*"', blob)
    assert outs[0]["verdicts"][0]["soundness"] == "fails"  # Nd4?? allows Qxf7#


def test_tool_schemas_are_strict():
    for t in TOOL_SCHEMAS:
        sch = t["input_schema"]
        assert sch["additionalProperties"] is False and set(sch["required"]) == set(sch["properties"])
    with pytest.raises(ToolValidationError):
        validate_args("get_board_sector_tension", {"sector": "kingside", "reveal_best_move": True})
    with pytest.raises(ToolValidationError):
        validate_args("trigger_blindfold_mask", {"level": 9, "reason": "x"})
    with pytest.raises(ToolValidationError):
        validate_args("validate_user_hypothesis", '{"hypothesis_type": "best_move", "moves": [], "after_move": ""}')
