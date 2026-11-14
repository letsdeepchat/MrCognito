"""Phase 2 - Pattern recognition & impulse inhibition under constraints."""
from __future__ import annotations

from datetime import datetime, timedelta

import chess

from ..coach.hints import HintLadder
from ..coach.tools import parse_san
from ..engines.tactics import MOTIFS, assess_move
from .common import ApiError, Ctx, give_hint, masked_board, post_mortem_move, public_pm, start_post_mortem
from .content import pick_puzzles
from .phases import Phase, items_for, phase_seconds

EQUIV = [{"pin", "skewer"}, {"overloading", "deflection"}, {"back_rank", "mating_attack"},
         {"hanging_piece", "trapped_piece"}]
MAX_FAILED_MOVES = 3  # move budget; exhausting it opens the guided post-mortem


def motif_matches(guess: str, truth: str) -> bool:
    return guess == truth or any(guess in s and truth in s for s in EQUIV)


async def init(ctx: Ctx) -> dict:
    rng = ctx.rng()
    n = items_for(Phase.PATTERN_INHIBITION, ctx.session.budget_minutes)
    diff = ctx.st.get("difficulty", ctx.profile.difficulty_rating)
    per_item = max(45, phase_seconds(ctx.session.budget_minutes, Phase.PATTERN_INHIBITION) // max(1, n))
    items = []
    for p in pick_puzzles(diff, n, rng):
        distractors = [m for m in MOTIFS if not motif_matches(m, p["motif"])]
        rng.shuffle(distractors)
        options = distractors[:3] + [p["motif"]]
        rng.shuffle(options)
        items.append({"puzzle": p, "options": options, "step": "diagnose", "ladder": {}, "hint_tier": 0,
                      "user_strings": [], "fell_for_trap": False, "solved": None, "time_budget_s": per_item,
                      "started_at": None, "pm": None})
    st = {"items": items, "idx": 0, "done": not items}
    _start_item(ctx, st)
    return st


def _start_item(ctx: Ctx, st: dict) -> None:
    if not st["done"]:
        st["items"][st["idx"]]["started_at"] = ctx.iso()


def _deadline(it: dict) -> datetime:
    return datetime.fromisoformat(it["started_at"]) + timedelta(seconds=it["time_budget_s"])


def view(ctx: Ctx, st: dict) -> dict:
    if st["done"]:
        return {"done": True}
    it = st["items"][st["idx"]]
    p = it["puzzle"]
    ladder = HintLadder.from_dict(it["ladder"])
    v = {"item": st["idx"] + 1, "items": len(st["items"]), "step": it["step"],
         "board": masked_board(p["fen"], ctx.session.mask_level,
                               "white" if chess.Board(p["fen"]).turn else "black"),
         "motif_options": it["options"], "attempts_left": max(0, MAX_FAILED_MOVES - it.get("failed_moves", 0)),
         "time_budget_s": it["time_budget_s"], "item_deadline": _deadline(it).isoformat(), "hint_tier": it["hint_tier"],
         "instruction": ("Before touching a piece: name the motif, and name the move most players would be tempted "
                         "by and why it fails." if it["step"] == "diagnose" else "Now find the move that works.")}
    if it.get("diagnosis_feedback"):
        v["diagnosis_feedback"] = it["diagnosis_feedback"]
    if it.get("tier3_pair") and it["hint_tier"] >= 3:
        v["candidates"] = it["tier3_pair"]
    if it["pm"]:
        v["post_mortem"] = public_pm(it["pm"])
    return v


async def _finish_item(ctx: Ctx, st: dict, it: dict, solved: bool) -> dict:
    it["solved"] = solved
    it["step"] = "done"
    p = it["puzzle"]
    ctx.score("trap", 0.0 if it["fell_for_trap"] else 1.0)
    st["idx"] += 1
    if st["idx"] >= len(st["items"]):
        st["done"] = True
    else:
        _start_item(ctx, st)
    b = chess.Board(p["fen"])
    tempting_san = b.san(chess.Move.from_uci(p["tempting_uci"]))
    # The item is over: naming the motif and the *refuted* tempting move is safe consolidation feedback.
    return {"item_complete": True, "solved": solved, "motif": p["motif"],
            "debrief": f"The tempting {tempting_san} fails to a {p['motif'].replace('_', ' ')}. "
                       "Next time, ask what your opponent's most forcing reply would be before you commit."}


async def act(ctx: Ctx, st: dict, action: dict) -> dict:
    if st["done"]:
        raise ApiError(409, "phase_done", "Phase complete.")
    it = st["items"][st["idx"]]
    p = it["puzzle"]
    board = chess.Board(p["fen"])
    ladder = HintLadder.from_dict(it["ladder"])
    t = action["type"]
    if ctx.now > _deadline(it) and it["step"] in ("diagnose", "move"):
        ctx.outcome(0.0, it["time_budget_s"] * 1000)
        res = await _finish_item(ctx, st, it, False)
        res["timed_out"] = True
        return res
    if t == "pattern_diagnose":
        if it["step"] != "diagnose":
            raise ApiError(409, "already_diagnosed", "Diagnosis already recorded.")
        tm = parse_san(board, action.get("tempting_move", ""))
        motif_ok = motif_matches(action["motif"], p["motif"])
        tempt_ok = tm is not None and tm.uci() == p["tempting_uci"]
        if action.get("tempting_move"):
            it["user_strings"].append(action["tempting_move"])
        it["diagnosis"] = {"motif": action["motif"], "motif_correct": motif_ok, "tempting_correct": tempt_ok,
                           "why": action.get("why", "")[:400]}
        it["diagnosis_feedback"] = {
            "motif": "Your label fits the position." if motif_ok else "Reconsider the label once you see the opponent's best reply.",
            "tempting": "Yes, that's the move most players reach for." if tempt_ok else
                        "Most players would be drawn to a different move. Which one looks most natural?",
        }
        if not motif_ok:
            ladder.record_attempt()
            it["ladder"] = ladder.to_dict()
        it["step"] = "move"
        it["move_started_at"] = ctx.iso()
        await ctx.log_turn(item_kind="pattern_diagnosis", fen=p["fen"], correct=motif_ok,
                           user_verbalized_plan=action.get("why"), extra={"motif": action["motif"], "tempting_correct": tempt_ok})
        return {"diagnosis_feedback": it["diagnosis_feedback"]}
    if t == "pattern_move":
        if it["step"] == "diagnose":
            raise ApiError(423, "diagnose_first", "Name the motif and the tempting move before touching a piece.")
        if it["step"] != "move":
            raise ApiError(409, "bad_step", "Use the post-mortem walkthrough now.")
        try:
            mv = chess.Move.from_uci(action["uci"])
        except ValueError:
            raise ApiError(422, "bad_move", "Moves are sent in UCI.")
        if mv not in board.legal_moves:
            raise ApiError(422, "illegal_move", "That move is not legal here.")
        think = ctx.think_ms(action.get("think_ms"), it.get("move_started_at"))
        it["user_strings"].extend([mv.uci(), board.san(mv)])
        top1 = mv.uci() == p["solution_uci"]
        ctx.st.setdefault("integrity", {}).setdefault("samples", []).append([think, 1 if top1 else 0])
        if mv.uci() == p["tempting_uci"]:
            ok, delta = False, p.get("drop_cp", 300)
            it["fell_for_trap"] = True
        elif mv.uci() in p["acceptable_ucis"]:
            ok, delta = True, 0
        else:
            a = await assess_move(ctx.engine, board, mv, depth=ctx.depth)
            ok, delta = a.delta_cp < 60, a.delta_cp
        await ctx.log_turn(item_kind="pattern_move", fen=p["fen"], user_move_uci=mv.uci(), correct=ok,
                           stockfish_eval_delta_cp=int(delta), hint_tier_used=it["hint_tier"], think_time_ms=think,
                           matched_engine_top1=top1, extra={"tempting": mv.uci() == p["tempting_uci"]})
        if ok:
            ctx.outcome(1.0 if it["hint_tier"] == 0 else max(0.25, 1 - 0.25 * it["hint_tier"]), think)
            return await _finish_item(ctx, st, it, True)
        ladder.record_attempt()
        it["ladder"] = ladder.to_dict()
        it["failed_moves"] = it.get("failed_moves", 0) + 1
        if it["failed_moves"] >= MAX_FAILED_MOVES:
            ctx.outcome(0.0, think)
            it["step"] = "post_mortem"
            it["hint_tier"] = 4
            it["pm"] = await start_post_mortem(ctx, board, p["solution_uci"])
            return {"correct": False, "move_budget_exhausted": True,
                    "message": "Move budget used. Let's walk the forcing line; you enter every move.",
                    "post_mortem": public_pm(it["pm"])}
        msg = ("Most players reach for that one. What is your opponent's most forcing answer to it?"
               if mv.uci() == p["tempting_uci"] else "That doesn't hold up. What does it leave undefended?")
        return {"correct": False, "attempts_left": MAX_FAILED_MOVES - it["failed_moves"], "message": msg}
    if t == "post_mortem_move":
        if not it["pm"]:
            raise ApiError(409, "no_post_mortem", "No walkthrough in progress.")
        res = await post_mortem_move(ctx, it["pm"], action["uci"])
        if it["pm"]["done"]:
            res.update(await _finish_item(ctx, st, it, False))
        return res
    raise ApiError(409, "bad_action", f"{t} is not valid in phase 2.")


async def hint(ctx: Ctx, st: dict, requested_tier: int | None, forfeit: bool) -> dict:
    it = st["items"][st["idx"]]
    if it["step"] not in ("diagnose", "move"):
        raise ApiError(409, "no_hint", "No hint available in this step.")
    p = it["puzzle"]
    board = chess.Board(p["fen"])
    res = await give_hint(ctx, it, board, {"best_uci": p["solution_uci"], "acceptable": p["acceptable_ucis"],
                                           "flawed_uci": p["tempting_uci"], "user_strings": it["user_strings"]},
                          requested_tier, forfeit)
    if res["tier"] == 4:
        it["step"] = "post_mortem"
        ctx.outcome(0.0)
    return res


def current_board(st: dict) -> chess.Board | None:
    if st.get("done"):
        return None
    return chess.Board(st["items"][st["idx"]]["puzzle"]["fen"])


def solving(st: dict) -> dict | None:
    if st.get("done"):
        return None
    it = st["items"][st["idx"]]
    p = it["puzzle"]
    return {"best_uci": p["solution_uci"], "user_strings": it["user_strings"]}
