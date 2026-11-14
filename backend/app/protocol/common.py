"""Shared machinery for the phase handlers: context, errors, masking, hints, post-mortems."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import chess
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..clock import as_utc
from ..coach.guardrails import board_move_strings, tier_policy
from ..coach.hints import HintDenied, HintLadder
from ..coach.orchestrator import CoachReply, SocraticOrchestrator
from ..coach.rag import get_store
from ..coach.tools import ToolContext
from ..config import get_settings
from ..db import CognitiveEvent, CognitiveProfile, TrainingSession, TurnTelemetry
from ..engines.base import ChessEngine
from ..engines.human_model import HumanMoveModel
from ..engines.tactics import most_tense_sector, position_concepts
from .. import telemetry as tm


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass
class Ctx:
    db: AsyncSession
    session: TrainingSession
    profile: CognitiveProfile
    engine: ChessEngine
    human: HumanMoveModel
    coach: SocraticOrchestrator
    now: datetime

    @property
    def st(self) -> dict:
        return self.session.phase_state

    @property
    def ph(self) -> dict:
        return self.session.phase_state.setdefault("phase", {})

    def touch(self) -> None:
        flag_modified(self.session, "phase_state")

    def rng(self) -> random.Random:
        c = self.st.get("rng_counter", 0) + 1
        self.st["rng_counter"] = c
        return random.Random(f"{self.session.id}:{c}")

    @property
    def depth(self) -> int:
        return get_settings().analysis_depth

    def elapsed_s(self) -> float:
        return (self.now - as_utc(self.session.started_at)).total_seconds()

    def iso(self) -> str:
        return self.now.isoformat()

    def think_ms(self, client_ms: int | None, started_iso: str | None) -> int:
        server = int((self.now - datetime.fromisoformat(started_iso)).total_seconds() * 1000) if started_iso else None
        if client_ms is None:
            return server or 0
        return int(min(client_ms, server)) if server is not None else int(client_ms)

    # --- scoring --------------------------------------------------------
    def score(self, key: str, value: float) -> None:
        self.st.setdefault("scores", {}).setdefault(key, []).append(round(float(value), 4))

    def outcome(self, success: float, latency_ms: int | None = None) -> None:
        self.score("outcomes", success)
        self.st["difficulty"] = tm.staircase(self.st.get("difficulty", self.profile.difficulty_rating), success)
        if latency_ms is not None:
            fat = self.st.setdefault("fatigue", [])
            fat.append([round(self.elapsed_s(), 1), int(latency_ms), 1 if success >= 0.5 else 0])
            sig = tm.fatigue(fat)
            if sig.fatigued and not self.st.get("fatigue_flag_at") == len(fat):
                self.st["difficulty"] = max(600.0, self.st["difficulty"] - 60)
                self.st["fatigue_notice"] = ("Response times are rising while accuracy dips. Slow down, check threats "
                                             "out loud, and prefer the simpler plan. Difficulty eased slightly.")
                self.st["fatigue_flag_at"] = len(fat)

    async def log_turn(self, **kw: Any) -> None:
        self.db.add(TurnTelemetry(session_id=self.session.id, phase=self.session.current_phase, **kw))

    async def event(self, kind: str, payload: dict) -> None:
        self.db.add(CognitiveEvent(session_id=self.session.id, kind=kind, payload=payload))


# --- board masking ----------------------------------------------------------
def masked_board(fen: str, level: int, orientation: str = "white") -> dict:
    """Server-side masking so hidden information never reaches the client.

    0: full position (FEN)   1: monochrome silhouettes (type, no colour)
    2: coordinate markers (occupancy only)   3: empty board
    """
    b = chess.Board(fen)
    pieces: dict[str, str] = {}
    if level <= 2:
        for sq, pc in b.piece_map().items():
            name = chess.square_name(sq)
            pieces[name] = pc.symbol() if level == 0 else pc.symbol().upper() if level == 1 else "*"
    out = {"mask_level": level, "pieces": pieces, "orientation": orientation,
           "side_to_move": "white" if b.turn else "black"}
    if level == 0:
        out["fen"] = fen
    return out


def sector_phrase(board: chess.Board, move: chess.Move) -> tuple[str, str]:
    """(human phrase, tension-tool sector id) localising a move without naming it."""
    pt = board.piece_type_at(move.from_square)
    f0, r0 = chess.square_file(move.from_square), chess.square_rank(move.from_square)
    f1, r1 = chess.square_file(move.to_square), chess.square_rank(move.to_square)
    if pt in (chess.BISHOP, chess.QUEEN) and abs(f1 - f0) == abs(r1 - r0) and f1 != f0:
        df, dr = (1 if f1 > f0 else -1), (1 if r1 > r0 else -1)
        a = move.from_square
        while 0 <= chess.square_file(a) - df <= 7 and 0 <= chess.square_rank(a) - dr <= 7:
            a = chess.square(chess.square_file(a) - df, chess.square_rank(a) - dr)
        b = move.from_square
        while 0 <= chess.square_file(b) + df <= 7 and 0 <= chess.square_rank(b) + dr <= 7:
            b = chess.square(chess.square_file(b) + df, chess.square_rank(b) + dr)
        lo, hi = sorted([chess.square_name(a), chess.square_name(b)], key=lambda s: (s[1], s[0]))
        return f"{lo}–{hi} diagonal", f"{lo}-{hi}"
    if pt in (chess.ROOK, chess.QUEEN) and f0 == f1:
        return f"{'abcdefgh'[f0]}-file", f"{'abcdefgh'[f0]}-file"
    if pt in (chess.ROOK, chess.QUEEN) and r0 == r1:
        return f"rank {r0 + 1}", f"rank-{r0 + 1}"
    side = "queenside" if f1 <= 2 else "kingside" if f1 >= 5 else "center"
    half = "your own half" if (r1 <= 3) == board.turn else "the opponent's half"
    return f"{side} ({half} of the board)", side


IMBALANCE_PROMPTS = {
    "king_safety": "Compare how safe each side's monarch is, and how many attackers could join quickly.",
    "loose_pieces": "Compare which side has more undefended units right now.",
    "back_rank": "Ask what would happen if one side's first rank were invaded.",
    "passed_pawn": "Weigh the long-term trumps: which side owns the most dangerous runner?",
    "open_file": "Notice which lines are open and whose heavy units could use them first.",
    "isolated_pawn": "Weigh structure against activity: who benefits from trades here?",
    "bishop_pair": "Compare the long-range units on each side and whether the position is opening.",
    "endgame": "Compare monarch activity and the most advanced runners for both sides.",
}


def imbalance_prompt(concepts: list[str]) -> str:
    for c in concepts:
        if c in IMBALANCE_PROMPTS:
            return IMBALANCE_PROMPTS[c]
    return "Compare the activity of both sides' worst-placed units."


async def rag_for(board: chess.Board, extra: list[str] | None = None, k: int = 2) -> list[dict]:
    tags = position_concepts(board) + (extra or [])
    chunks = await get_store().search(" ".join(tags), k=k, tags=tags)
    return [{"id": c.id, "title": c.title, "principle": c.text, "question": c.question} for c in chunks]


def policy_sets(board: chess.Board, best_uci: str | None, user_strings: list[str], extra_allowed: list[str] | None = None):
    forbidden: set[str] = set()
    if best_uci:
        mv = chess.Move.from_uci(best_uci)
        forbidden = board_move_strings(board, mv)
        if board.piece_type_at(mv.from_square) == chess.PAWN and not board.is_capture(mv):
            forbidden.add(chess.square_name(mv.to_square))
    allowed = {s.strip().rstrip("+#") for s in user_strings if s and s.strip()} | set(extra_allowed or [])
    if board.move_stack:
        prev = board.copy()
        last = prev.pop()
        allowed |= board_move_strings(prev, last)
    return allowed, forbidden


async def coach_reply(ctx: Ctx, board: chess.Board, task: dict, tier: int, allowed: set[str], forbidden: set[str]) -> CoachReply:
    async def on_mask(level: int, reason: str) -> None:
        ctx.session.mask_level = max(ctx.session.mask_level, level)
        await ctx.event("mask_raised", {"level": level, "reason": reason})

    async def on_log(event: str, payload: dict) -> None:
        await ctx.event(f"coach:{event}", payload)

    tool_ctx = ToolContext(board=board, engine=ctx.engine, depth=min(ctx.depth, 12), on_mask=on_mask, on_log=on_log)
    task.setdefault("focus_sector", most_tense_sector(board))
    task.setdefault("rag", await rag_for(board))
    return await ctx.coach.respond(task, tier_policy(tier, allowed, forbidden), tool_ctx)


# --- hint ladder ---------------------------------------------------------------
async def give_hint(ctx: Ctx, holder: dict, board: chess.Board, solving: dict, requested_tier: int | None,
                    forfeit: bool) -> dict:
    """``holder`` owns the ladder; ``solving`` = {best_uci, acceptable, flawed_uci, user_strings, motif}."""
    ladder = HintLadder.from_dict(holder.get("ladder"))
    try:
        tier = ladder.advance(requested_tier, forfeit)
    except HintDenied as exc:
        raise ApiError(409, exc.code, exc.message, current_tier=ladder.tier)
    holder["ladder"] = ladder.to_dict()
    holder["hint_tier"] = tier
    best = chess.Move.from_uci(solving["best_uci"]) if solving.get("best_uci") else None
    allowed, forbidden = policy_sets(board, solving.get("best_uci"), solving.get("user_strings", []))
    task: dict = {"kind": "hint", "tier": tier, "phase": ctx.session.current_phase}
    out: dict = {"tier": tier, "tier_name": {1: "conceptual_orientation", 2: "sector_localization",
                                            3: "candidate_comparison", 4: "guided_post_mortem"}[tier]}
    concepts = position_concepts(board)
    if tier == 1:
        task["imbalance_prompt"] = imbalance_prompt(concepts)
    elif tier == 2 and best is not None:
        phrase, sec = sector_phrase(board, best)
        task["sector_phrase"], task["focus_sector"] = phrase, sec
    elif tier == 3:
        pair = candidate_pair(board, solving, ctx.rng())
        task["candidate_pair"] = pair
        allowed |= set(pair)
        out["candidates"] = pair
        holder["tier3_pair"] = pair
    elif tier == 4:
        holder["pm"] = await start_post_mortem(ctx, board, solving["best_uci"])
        out["post_mortem"] = public_pm(holder["pm"])
    reply = await coach_reply(ctx, board, task, tier, allowed, forbidden)
    out.update({"message": reply.message, "question": reply.question, "source": reply.source})
    await ctx.event("hint", {"tier": tier, "phase": ctx.session.current_phase})
    return out


def candidate_pair(board: chess.Board, solving: dict, rng: random.Random) -> list[str]:
    best = solving.get("best_uci")
    acceptable = [u for u in solving.get("acceptable", []) if u != best]
    sound = acceptable[0] if acceptable else best
    flawed = solving.get("flawed_uci")
    if not flawed or flawed == sound:
        legal = [m.uci() for m in board.legal_moves if m.uci() not in solving.get("acceptable", []) and m.uci() != best]
        flawed = legal[0] if legal else sound
    pair = [board.san(chess.Move.from_uci(sound)), board.san(chess.Move.from_uci(flawed))]
    rng.shuffle(pair)
    return pair


# --- guided post-mortem (tier 4) -------------------------------------------
async def start_post_mortem(ctx: Ctx, board: chess.Board, best_uci: str) -> dict:
    b = board.copy(stack=False)
    an = await ctx.engine.analyse(b, depth=ctx.depth)
    pv = [m.uci() for m in (an.best.pv if an.best else ())][:5] or [best_uci]
    if pv[0] != best_uci:
        pv = [best_uci]
    return {"fen": board.fen(), "line": pv, "idx": 0, "misses": 0, "done": False, "played": []}


def pm_board(pm: dict) -> chess.Board:
    b = chess.Board(pm["fen"])
    for u in pm["line"][: pm["idx"]]:
        b.push(chess.Move.from_uci(u))
    return b


def public_pm(pm: dict) -> dict:
    b = pm_board(pm)
    return {"fen": b.fen(), "step": pm["idx"] // 2 + 1, "steps": (len(pm["line"]) + 1) // 2, "done": pm["done"],
            "played_san": pm["played"]}


async def post_mortem_move(ctx: Ctx, pm: dict, uci: str) -> dict:
    """User must input every move of the forcing line; misses earn localisation, then the move to enter."""
    if pm.get("done"):
        raise ApiError(409, "post_mortem_done", "The walkthrough is finished.")
    b = pm_board(pm)
    expected = chess.Move.from_uci(pm["line"][pm["idx"]])
    try:
        mv = chess.Move.from_uci(uci)
    except ValueError:
        raise ApiError(422, "bad_move", "Moves are sent in UCI (e.g. e2e4).")
    if mv not in b.legal_moves:
        raise ApiError(422, "illegal_move", "That move is not legal here.")
    res: dict = {}
    if mv == expected:
        pm["played"].append(b.san(mv))
        b.push(mv)
        pm["idx"] += 1
        pm["misses"] = 0
        if pm["idx"] < len(pm["line"]):
            reply = chess.Move.from_uci(pm["line"][pm["idx"]])
            res["opponent_reply_san"] = b.san(reply)
            pm["played"].append(b.san(reply))
            pm["idx"] += 1
        res["correct"] = True
        res["message"] = "Correct. Keep going: what is the next forcing move?"
        if pm["idx"] >= len(pm["line"]):
            pm["done"] = True
            res["message"] = "That's the whole forcing sequence. Say in one sentence why it works."
    else:
        pm["misses"] += 1
        res["correct"] = False
        if pm["misses"] == 1:
            phrase, _ = sector_phrase(b, expected)
            res["message"] = f"Not that one. The key move happens on the {phrase}. Which forcing move lives there?"
        else:
            # Post-mortem (solving has ended by forfeit / exhausted budget): the user still enters it by hand.
            res["post_mortem_reveal"] = b.san(expected)
            res["message"] = f"The move is {b.san(expected)}. Enter it on the board yourself, and say what it threatens."
    res["post_mortem"] = public_pm(pm)
    return res
