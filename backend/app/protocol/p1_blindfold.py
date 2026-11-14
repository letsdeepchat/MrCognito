"""Phase 1 - Visuospatial working memory & blindfold warm-up."""
from __future__ import annotations

import random

import chess

from .common import ApiError, Ctx, masked_board
from .content import fragment
from .phases import Phase, items_for

PIECE_NAMES = {chess.QUEEN: "queen", chess.ROOK: "rook", chess.BISHOP: "bishop", chess.KNIGHT: "knight", chess.KING: "king"}
COLOR = {chess.WHITE: "White", chess.BLACK: "Black"}


def _bishop_reach(board: chess.Board, sq: chess.Square) -> set[chess.Square]:
    color = board.color_at(sq)
    one = {t for t in board.attacks(sq) if board.color_at(t) != color}
    two: set[chess.Square] = set()
    for t in one:
        b = board.copy(stack=False)
        b.remove_piece_at(sq)
        b.set_piece_at(t, chess.Piece(chess.BISHOP, color))
        two |= {u for u in b.attacks(t) if b.color_at(u) != color}
    return (one | two) - {sq}


def make_queries(board: chess.Board, rng: random.Random, k: int = 3) -> list[dict]:
    qs: list[dict] = []
    # 1. where is a uniquely identifiable piece?
    cands = []
    for color in (chess.WHITE, chess.BLACK):
        for pt in (chess.QUEEN, chess.KING, chess.KNIGHT, chess.ROOK):
            sqs = list(board.pieces(pt, color))
            if len(sqs) == 1:
                cands.append((color, pt, sqs[0], PIECE_NAMES[pt]))
        for sq in board.pieces(chess.BISHOP, color):
            shade = "light" if (chess.square_file(sq) + chess.square_rank(sq)) % 2 else "dark"
            same = [s for s in board.pieces(chess.BISHOP, color) if (chess.square_file(s) + chess.square_rank(s)) % 2 == (chess.square_file(sq) + chess.square_rank(sq)) % 2]
            if len(same) == 1:
                cands.append((color, chess.BISHOP, sq, f"{shade}-squared bishop"))
    if cands:
        color, pt, sq, name = rng.choice(cands)
        qs.append({"kind": "square", "prompt": f"On which square is {COLOR[color]}'s {name} now?",
                   "answer": chess.square_name(sq)})
    # 2. attack count on a contested square
    squares = [s for s in chess.SQUARES if len(board.attackers(chess.WHITE, s)) + len(board.attackers(chess.BLACK, s)) >= 2]
    if squares:
        s = rng.choice(squares)
        color = rng.choice([chess.WHITE, chess.BLACK])
        qs.append({"kind": "count", "prompt": f"How many {COLOR[color]} pieces attack {chess.square_name(s)}?",
                   "answer": len(board.attackers(color, s))})
    # 3. two-move bishop reach
    bishops = [(c, s) for c in (chess.WHITE, chess.BLACK) for s in board.pieces(chess.BISHOP, c)]
    if bishops:
        c, s = rng.choice(bishops)
        shade = (chess.square_file(s) + chess.square_rank(s)) % 2
        same_colour = [t for t in chess.SQUARES if (chess.square_file(t) + chess.square_rank(t)) % 2 == shade and t != s]
        reach = _bishop_reach(board, s)
        pool = list(reach) if rng.random() < 0.5 and reach else [t for t in same_colour if t not in reach] or same_colour
        target = rng.choice(pool)
        shade_name = "light" if shade else "dark"
        qs.append({"kind": "yesno", "prompt": f"Can {COLOR[c]}'s {shade_name}-squared bishop reach "
                                              f"{chess.square_name(target)} in two moves (other pieces stay put)?",
                   "answer": "yes" if target in reach else "no"})
    # 4. doubly attacked square in a sector
    color = rng.choice([chess.WHITE, chess.BLACK])
    sector = rng.choice(["kingside", "queenside", "center"])
    files = {"queenside": (0, 1, 2), "center": (3, 4), "kingside": (5, 6, 7)}[sector]
    doubly = sorted(chess.square_name(s) for s in chess.SQUARES
                    if chess.square_file(s) in files and len(board.attackers(color, s)) >= 2)
    qs.append({"kind": "square_set", "prompt": f"Name one {sector} square attacked by two or more {COLOR[color]} "
                                               f"pieces (or answer 'none').", "answer": doubly})
    rng.shuffle(qs)
    for i, q in enumerate(qs[:k]):
        q["id"] = f"q{i + 1}"
    return qs[:k]


def check_answer(q: dict, answer: str) -> bool:
    a = (answer or "").strip().lower()
    if q["kind"] == "square":
        return a == q["answer"]
    if q["kind"] == "count":
        try:
            return int(a) == q["answer"]
        except ValueError:
            return False
    if q["kind"] == "yesno":
        return a in ("yes", "y", "true") if q["answer"] == "yes" else a in ("no", "n", "false")
    if q["kind"] == "square_set":
        return (a in ("none", "no", "0") and not q["answer"]) or a in q["answer"]
    return False


def public_queries(qs: list[dict]) -> list[dict]:
    return [{"id": q["id"], "kind": q["kind"], "prompt": q["prompt"]} for q in qs]


async def init(ctx: Ctx) -> dict:
    rng = ctx.rng()
    diff = ctx.st.get("difficulty", ctx.profile.difficulty_rating)
    n_moves = max(3, min(6, 3 + int((diff - 800) // 400)))
    items = []
    for i in range(items_for(Phase.BLINDFOLD_WARMUP, ctx.session.budget_minutes)):
        fr = fragment(rng, n_moves)
        b = chess.Board(fr["start_fen"])
        for san in fr["moves"][:-1]:
            b.push_san(san)
        final = b.parse_san(fr["moves"][-1])
        items.append({
            "opening": fr["opening"], "start_fen": fr["start_fen"], "dictated": fr["moves"][:-1],
            "final_san": fr["moves"][-1], "final_uci": final.uci(), "position_fen": b.fen(),
            "queries": make_queries(b, rng, 3 if diff >= 1000 else 2), "answers": {}, "final_tries": 0,
            "mask_level": min(3, 1 + i + (1 if diff > 1500 else 0)), "step": "study", "started_at": ctx.iso(),
        })
    return {"items": items, "idx": 0, "done": False}


def view(ctx: Ctx, st: dict) -> dict:
    if st["done"]:
        return {"done": True}
    it = st["items"][st["idx"]]
    v = {"item": st["idx"] + 1, "items": len(st["items"]), "step": it["step"], "opening": it["opening"],
         "mask_level": it["mask_level"]}
    if it["step"] == "study":
        v["board"] = masked_board(it["start_fen"], 0)
        v["instruction"] = "Study this position. When you press ready, the pieces fade and moves are dictated."
    elif it["step"] == "dictation":
        v["board"] = masked_board(it["start_fen"], it["mask_level"])
        v["dictated_moves"] = it["dictated"]
        v["instruction"] = "Update the position in your head as each move is read out."
    elif it["step"] == "queries":
        v["board"] = masked_board(it["start_fen"], it["mask_level"])
        v["dictated_moves"] = it["dictated"]
        v["queries"] = [q for q in public_queries(it["queries"]) if q["id"] not in it["answers"]]
    elif it["step"] == "final":
        v["board"] = masked_board(it["position_fen"], max(2, it["mask_level"]))
        v["instruction"] = f"Now play {it['final_san']} on the board, from memory."
    return v


async def act(ctx: Ctx, st: dict, action: dict) -> dict:
    it = st["items"][st["idx"]]
    t = action["type"]
    if t == "blindfold_ready" and it["step"] == "study":
        it["step"] = "dictation"
        return {"ok": True}
    if t == "blindfold_dictation_done" and it["step"] == "dictation":
        it["step"] = "queries"
        it["queries_started_at"] = ctx.iso()
        return {"ok": True}
    if t == "blindfold_answer" and it["step"] == "queries":
        q = next((q for q in it["queries"] if q["id"] == action["query_id"]), None)
        if q is None or q["id"] in it["answers"]:
            raise ApiError(409, "bad_query", "Unknown or already answered query.")
        ok = check_answer(q, action["answer"])
        it["answers"][q["id"]] = {"answer": action["answer"], "correct": ok}
        latency = ctx.think_ms(action.get("think_ms"), it.get("queries_started_at"))
        ctx.score("wm", 1.0 if ok else 0.0)
        ctx.outcome(1.0 if ok else 0.0, latency)
        await ctx.log_turn(item_kind="blindfold_query", fen=it["position_fen"], correct=ok, think_time_ms=latency,
                           extra={"kind": q["kind"], "mask_level": it["mask_level"]})
        if len(it["answers"]) == len(it["queries"]):
            it["step"] = "final"
            it["final_started_at"] = ctx.iso()
        expected = q["answer"] if not isinstance(q["answer"], list) else (q["answer"] or ["none"])
        return {"correct": ok, "expected": expected}
    if t == "blindfold_final_move" and it["step"] == "final":
        it["final_tries"] += 1
        ok = action["uci"] == it["final_uci"]
        latency = ctx.think_ms(action.get("think_ms"), it.get("final_started_at"))
        if ok or it["final_tries"] >= 2:
            ctx.score("wm", 1.0 if ok and it["final_tries"] == 1 else 0.5 if ok else 0.0)
            ctx.outcome(1.0 if ok else 0.0, latency)
            await ctx.log_turn(item_kind="blindfold_final", fen=it["position_fen"], user_move_uci=action["uci"],
                               correct=ok, think_time_ms=latency, extra={"tries": it["final_tries"]})
            it["step"] = "done"
            st["idx"] += 1
            if st["idx"] >= len(st["items"]):
                st["done"] = True
            return {"correct": ok, "revealed_position": masked_board(it["position_fen"], 0)}
        return {"correct": False, "message": "Not quite. Rebuild the position from the kings outward and try once more."}
    raise ApiError(409, "bad_action", f"{t} is not valid in step {it['step']}.")


def current_board(st: dict) -> chess.Board | None:
    if st.get("done"):
        return None
    return chess.Board(st["items"][st["idx"]]["position_fen"])


def solving(st: dict) -> dict | None:
    return None
