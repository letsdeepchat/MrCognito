"""Phase 4 - Dynamic adaptability & rule-perturbation drills."""
from __future__ import annotations

from datetime import datetime, timedelta

import chess

from .common import ApiError, Ctx, masked_board
from .content import PERTURBATION_ENDGAMES
from .phases import Phase, items_for

KINDS = ["constraint_shift", "tempo_compression", "perspective_flip"]
PIECE_NAMES = {chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen"}
TEMPO_SECONDS = 45
OPP_DEPTH = 8


async def _eval(ctx: Ctx, board: chess.Board, color: chess.Color) -> int:
    if board.is_checkmate():
        return -10_000 if board.turn == color else 10_000
    if board.is_game_over():
        return 0
    an = await ctx.engine.analyse(board, depth=OPP_DEPTH)
    cp = an.best.cp if an.best else 0
    return cp if board.turn == color else -cp


async def _make_drill(ctx: Ctx, kind: str, rng) -> dict:
    last = ctx.st.get("sparring_last")
    if kind == "perspective_flip" and last and not chess.Board(last["fen"]).is_game_over():
        board = chess.Board(last["fen"])
        old = chess.WHITE if last["user_color"] == "white" else chess.BLACK
        if board.turn == old:  # let the "old self" move once, then the user defends the other side
            an = await ctx.engine.analyse(board, depth=OPP_DEPTH)
            if an.best:
                board.push(an.best.move)
        if board.is_game_over():
            kind = "constraint_shift"
    else:
        board = None
        if kind == "perspective_flip":
            fen, _ = rng.choice(PERTURBATION_ENDGAMES)
            b = chess.Board(fen)
            b.turn = not b.turn  # the user now defends the *other* side of a known endgame
            b.ep_square = None
            board = b if b.is_valid() and not b.is_game_over() else None
            if board is None:
                kind = "constraint_shift"
    if kind != "perspective_flip":
        fen, desc = rng.choice(PERTURBATION_ENDGAMES)
        board = chess.Board(fen)
    color = board.turn
    d: dict = {"kind": kind, "fen": board.fen(), "user_color": "white" if color else "black", "moves_made": 0,
               "max_moves": 5 if kind == "constraint_shift" else 5 if kind == "tempo_compression" else 4,
               "forbidden_piece": None, "deadline": None, "step": "deep" if kind == "tempo_compression" else "play",
               "flipped": kind == "perspective_flip", "last_opponent_san": None, "started_at": ctx.iso(), "violations": 0}
    if kind == "constraint_shift":
        types = [pt for pt in (chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.QUEEN) if board.pieces(pt, color)]
        # keep at least one other piece type able to move
        d["forbidden_piece"] = PIECE_NAMES[types[0]] if types else None
    d["start_eval"] = await _eval(ctx, board, color)
    return d


async def init(ctx: Ctx) -> dict:
    rng = ctx.rng()
    n = items_for(Phase.ADAPTABILITY_PERTURBATION, ctx.session.budget_minutes)
    start = rng.randrange(len(KINDS))
    drills = [await _make_drill(ctx, KINDS[(start + i) % len(KINDS)], rng) for i in range(n)]
    return {"drills": drills, "idx": 0, "done": False}


INSTRUCTIONS = {
    "constraint_shift": "Constraint shift: keep or improve your position for {n} moves without moving your {piece}.",
    "tempo_compression": "Deep think first: one untimed move. Then the clock compresses to 45 seconds for the rest.",
    "perspective_flip": "Perspective flip: the board has turned. Defend the side you were just playing against.",
}


def view(ctx: Ctx, st: dict) -> dict:
    if st["done"]:
        return {"done": True}
    d = st["drills"][st["idx"]]
    orient = d["user_color"]
    return {"drill": st["idx"] + 1, "drills": len(st["drills"]), "kind": d["kind"],
            "instruction": (INSTRUCTIONS[d["kind"]].format(n=d["max_moves"], piece=d["forbidden_piece"])
                            if d["forbidden_piece"] or d["kind"] != "constraint_shift"
                            else f"Constraint shift: convert within {d['max_moves']} moves, king and pawns only."),
            "board": masked_board(d["fen"], ctx.session.mask_level, orient), "flipped": d["flipped"],
            "forbidden_piece": d["forbidden_piece"], "moves_left": d["max_moves"] - d["moves_made"],
            "step": d["step"], "deadline": d["deadline"], "last_opponent_move": d["last_opponent_san"]}


async def _finish(ctx: Ctx, st: dict, d: dict, board: chess.Board, timed_out: bool = False) -> dict:
    color = chess.WHITE if d["user_color"] == "white" else chess.BLACK
    final = await _eval(ctx, board, color)
    drop = d["start_eval"] - final
    score = 1.0 if drop <= 100 else 0.5 if drop <= 250 else 0.0
    if timed_out:
        score = min(score, 0.5) * (d["moves_made"] / d["max_moves"])
    ctx.score("adapt", score)
    ctx.outcome(score)
    await ctx.log_turn(item_kind=f"perturb_{d['kind']}", fen=d["fen"], correct=score >= 0.5,
                       stockfish_eval_delta_cp=int(max(0, drop)), extra={"score": score, "timed_out": timed_out})
    st["idx"] += 1
    if st["idx"] >= len(st["drills"]):
        st["done"] = True
    verdict = "held" if score >= 1.0 else "partly held" if score > 0 else "slipped"
    return {"drill_complete": True, "result": verdict, "timed_out": timed_out}


async def act(ctx: Ctx, st: dict, action: dict) -> dict:
    if st["done"]:
        raise ApiError(409, "phase_done", "Phase complete.")
    if action["type"] != "perturb_move":
        raise ApiError(409, "bad_action", "Only moves are valid in phase 4.")
    d = st["drills"][st["idx"]]
    board = chess.Board(d["fen"])
    if d["deadline"] and ctx.now > datetime.fromisoformat(d["deadline"]):
        return await _finish(ctx, st, d, board, timed_out=True)
    try:
        mv = chess.Move.from_uci(action["uci"])
    except ValueError:
        raise ApiError(422, "bad_move", "Moves are sent in UCI.")
    if mv not in board.legal_moves:
        raise ApiError(422, "illegal_move", "That move is not legal here.")
    if d["forbidden_piece"]:
        pt = board.piece_type_at(mv.from_square)
        name = PIECE_NAMES.get(pt, "king" if pt == chess.KING else "pawn")
        if name == d["forbidden_piece"]:
            only = all((PIECE_NAMES.get(board.piece_type_at(m.from_square)) == d["forbidden_piece"]) for m in board.legal_moves)
            if not only:
                d["violations"] += 1
                raise ApiError(422, "constraint_violation", f"Your {name} is frozen for this drill. Re-assign its job.")
    think = ctx.think_ms(action.get("think_ms"), d["started_at"])
    board.push(mv)
    d["moves_made"] += 1
    await ctx.log_turn(item_kind=f"perturb_move_{d['kind']}", fen=d["fen"], user_move_uci=mv.uci(), think_time_ms=think)
    if d["step"] == "deep":
        d["step"] = "compressed"
        d["deadline"] = (ctx.now + timedelta(seconds=TEMPO_SECONDS)).isoformat()
    if board.is_game_over() or d["moves_made"] >= d["max_moves"]:
        d["fen"] = board.fen()
        return await _finish(ctx, st, d, board)
    an = await ctx.engine.analyse(board, depth=OPP_DEPTH)
    reply = an.best.move
    d["last_opponent_san"] = board.san(reply)
    board.push(reply)
    d["fen"] = board.fen()
    d["started_at"] = ctx.iso()
    res = {"opponent_move": d["last_opponent_san"], "opponent_move_uci": reply.uci()}
    if board.is_game_over():
        res.update(await _finish(ctx, st, d, board))
    return res


def current_board(st: dict) -> chess.Board | None:
    return None if st.get("done") else chess.Board(st["drills"][st["idx"]]["fen"])


def solving(st: dict) -> dict | None:
    return None
