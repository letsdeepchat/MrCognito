"""Phase 3 - Verbalised candidate-move Socratic sparring with a think-aloud gate."""
from __future__ import annotations

import chess
from sqlalchemy import select

from ..coach.hints import HintLadder
from ..coach.tools import calculation_depth, parse_san
from ..db import UserPosition
from ..engines.tactics import assess_move, null_move_threat
from .. import telemetry as tm
from .common import (ApiError, Ctx, coach_reply, give_hint, masked_board, policy_sets, post_mortem_move, public_pm)
from .content import pick_sparring
from .phases import Phase, items_for

MAX_PLAN_REJECTIONS = 2


async def _new_turn(ctx: Ctx, board: chess.Board) -> dict:
    an = await ctx.engine.analyse(board, depth=ctx.depth, multipv=3)
    best = an.best
    gap = (an.lines[0].cp - an.lines[1].cp) if len(an.lines) > 1 else 0
    pred = await ctx.human.predict(board, ctx.profile.estimated_human_elo, ctx.profile.estimated_human_elo + 100)
    trap = None
    for mv, p in pred.top(2):
        if best and mv != best.move and p >= 0.15:
            cp = an.cp_of(mv)
            if cp is None:
                b2 = board.copy(stack=False)
                b2.push(mv)
                a2 = await ctx.engine.analyse(b2, depth=ctx.depth)
                cp = -(a2.best.cp if a2.best else 0)
            if best.cp - cp >= 150:
                trap = mv.uci()
                break
    threat = await null_move_threat(ctx.engine, board, depth=max(6, ctx.depth - 4))
    first = not ctx.ph.get("history")
    critical = first or gap >= 80 or trap is not None or threat >= 150
    return {"fen": board.fen(), "critical": critical, "unlocked": not critical, "plan": None, "plan_rejections": 0,
            "plan_feedback": None, "ladder": {}, "hint_tier": 0, "user_strings": [], "pm": None,
            "best_uci": best.move.uci() if best else None,
            "acceptable": [ln.move.uci() for ln in an.lines if best and best.cp - ln.cp <= 60],
            "trap_uci": trap, "threat": threat >= 150, "started_at": ctx.iso()}


async def init(ctx: Ctx) -> dict:
    rng = ctx.rng()
    up = (await ctx.db.execute(select(UserPosition).where(UserPosition.user_id == ctx.profile.user_id,
                                                          UserPosition.consumed.is_(False)).limit(1))).scalar_one_or_none()
    if up is not None:
        up.consumed = True
        pos = {"id": f"user-{up.id}", "fen": up.fen, "theme": "your own position"}
    else:
        pos = pick_sparring(ctx.st.get("difficulty", ctx.profile.difficulty_rating), rng)
    board = chess.Board(pos["fen"])
    st = {"start_fen": pos["fen"], "theme": pos.get("theme", ""), "user_color": "white" if board.turn else "black",
          "opponent_elo": ctx.profile.estimated_human_elo + 100, "moves_made": 0,
          "max_moves": items_for(Phase.SPARRING_VERBALIZED, ctx.session.budget_minutes), "history": [],
          "last_opponent_san": None, "done": False}
    ctx.st["phase"] = st  # _new_turn reads history
    st["turn"] = await _new_turn(ctx, board)
    ctx.st["turning_point"] = {"fen": pos["fen"], "swing": -1}
    return st


def view(ctx: Ctx, st: dict) -> dict:
    if st["done"]:
        return {"done": True}
    t = st["turn"]
    v = {"board": masked_board(t["fen"], ctx.session.mask_level, st["user_color"]), "theme": st["theme"],
         "user_color": st["user_color"], "moves_made": st["moves_made"], "max_moves": st["max_moves"],
         "critical": t["critical"], "plan_required": t["critical"] and not t["unlocked"],
         "plan_feedback": t["plan_feedback"], "last_opponent_move": st["last_opponent_san"], "hint_tier": t["hint_tier"],
         "instruction": ("Critical moment. Before the board unlocks, say: two candidate moves, your opponent's strongest "
                         "reply, and the purpose of your choice." if t["critical"] and not t["unlocked"]
                         else "Play your move.")}
    if t.get("tier3_pair") and t["hint_tier"] >= 3:
        v["candidates"] = t["tier3_pair"]
    if t["pm"]:
        v["post_mortem"] = public_pm(t["pm"])
    return v


async def _plan(ctx: Ctx, st: dict, a: dict) -> dict:
    t = st["turn"]
    board = chess.Board(t["fen"])
    if t["unlocked"] and t["plan"] is not None:
        raise ApiError(409, "plan_accepted", "Your plan was accepted; play your move.")
    cands_raw = [c for c in a["candidates"] if c.strip()][:3]
    t["user_strings"].extend(cands_raw + [a.get("opponent_reply", "")] + list(a.get("variation") or []))
    cands = [m for m in (parse_san(board, c) for c in cands_raw) if m is not None]
    cands = list(dict.fromkeys(cands))
    chosen = parse_san(board, a.get("chosen") or "") or (cands[0] if cands else None)
    ladder = HintLadder.from_dict(t["ladder"])
    verdict: dict = {"decision": "coherent"}
    complete = len(cands) >= 2 and len((a.get("opponent_reply") or "").strip()) >= 2 and len((a.get("purpose") or "").split()) >= 3
    cdi = None
    if not complete:
        verdict = {"decision": "recalculate", "reason": "incomplete"}
    else:
        chosen_san = board.san(chosen)
        assess = await assess_move(ctx.engine, board, chosen, depth=ctx.depth)
        after = board.copy(stack=False)
        after.push(chosen)
        refut = assess.refutation[0] if assess.refutation else None
        forcing = refut is not None and (after.gives_check(refut) or after.is_capture(refut))
        user_reply = parse_san(after, a.get("opponent_reply", ""))
        if assess.delta_cp >= 150 and forcing and user_reply != refut:
            verdict = {"decision": "recalculate", "reason": "overlooked_forcing_reply", "flawed_candidate": chosen_san}
            await ctx.event("coach:overlooked_forcing_reply", {"motif": assess.refutation_motif})
        elif assess.delta_cp >= 100 and forcing and user_reply is not None and user_reply != refut:
            verdict = {"decision": "recalculate", "reason": "weak_reply_prediction",
                       "user_reply": a.get("opponent_reply", "").strip()}
        elif assess.delta_cp >= 60:
            verdict = {"decision": "caution", "flags": assess.flags}
        variation = [chosen_san] + ([a["opponent_reply"].strip()] if user_reply is not None else []) + list(a.get("variation") or [])
        depth = await calculation_depth(board, ctx.engine, variation[:8], depth=min(ctx.depth, 12))
        cdi = depth["accurate_plies"]
        ctx.score("cdi", cdi)
    t["plan"] = {"candidates": [m.uci() for m in cands], "chosen": chosen.uci() if chosen else None,
                 "opponent_reply": a.get("opponent_reply", ""), "purpose": a.get("purpose", ""),
                 "transcript": a.get("transcript", "")}
    if verdict["decision"] == "recalculate":
        t["plan_rejections"] += 1
        ladder.record_attempt()
        t["ladder"] = ladder.to_dict()
        if t["plan_rejections"] > MAX_PLAN_REJECTIONS:
            t["unlocked"] = True
            verdict["note"] = "unlocked_after_retries"
    else:
        t["unlocked"] = True
    allowed, forbidden = policy_sets(board, t["best_uci"], t["user_strings"])
    reply = await coach_reply(ctx, board, {"kind": "plan_feedback", "verdict": verdict, "phase": "SPARRING_VERBALIZED"},
                              0, allowed, forbidden)
    t["plan_feedback"] = {"decision": verdict["decision"], "message": reply.message, "question": reply.question,
                          "source": reply.source}
    plan_text = f"Candidates: {', '.join(cands_raw)} | Reply: {a.get('opponent_reply', '')} | Purpose: {a.get('purpose', '')}"
    await ctx.log_turn(item_kind="sparring_plan", fen=t["fen"], user_verbalized_plan=plan_text,
                       extra={"decision": verdict["decision"], "reason": verdict.get("reason"), "cdi": cdi})
    return {"plan_feedback": t["plan_feedback"], "move_unlocked": t["unlocked"]}


async def _move(ctx: Ctx, st: dict, a: dict) -> dict:
    t = st["turn"]
    board = chess.Board(t["fen"])
    if t["critical"] and not t["unlocked"]:
        raise ApiError(423, "plan_required", "Critical turn: state your 3-part plan before moving.")
    try:
        mv = chess.Move.from_uci(a["uci"])
    except ValueError:
        raise ApiError(422, "bad_move", "Moves are sent in UCI.")
    if mv not in board.legal_moves:
        raise ApiError(422, "illegal_move", "That move is not legal here.")
    think = ctx.think_ms(a.get("think_ms"), t["started_at"])
    assess = await assess_move(ctx.engine, board, mv, depth=ctx.depth)
    pred = await ctx.human.predict(board, ctx.profile.estimated_human_elo, st["opponent_elo"])
    rma = None
    if t["plan"]:
        cands = [chess.Move.from_uci(u) for u in t["plan"]["candidates"]]
        rma = tm.reasoning_alignment(board, mv, cands, t["plan"]["opponent_reply"],
                                     assess.refutation[0] if assess.refutation else None, t["plan"]["purpose"])
        ctx.score("rma", rma)
    if t["trap_uci"]:
        ctx.score("trap", 0.0 if mv.uci() == t["trap_uci"] else 1.0)
    success = 1.0 if assess.delta_cp < 60 else 0.5 if assess.delta_cp < 150 else 0.0
    ctx.outcome(success, think)
    if t["critical"]:
        ctx.st.setdefault("integrity", {}).setdefault("samples", []).append([think, 1 if assess.matched_top1 else 0])
    await ctx.log_turn(item_kind="sparring_move", fen=t["fen"], user_move_uci=mv.uci(),
                       user_verbalized_plan=(t["plan"] or {}).get("purpose"), stockfish_eval_delta_cp=assess.delta_cp,
                       maia_human_move_prob=round(pred.prob(mv), 4), hint_tier_used=t["hint_tier"], think_time_ms=think,
                       correct=success >= 0.5, matched_engine_top1=assess.matched_top1,
                       extra={"flags": assess.flags, "critical": t["critical"], "rma": rma})
    if assess.delta_cp > ctx.st["turning_point"]["swing"]:
        ctx.st["turning_point"] = {"fen": t["fen"], "swing": assess.delta_cp}
    user_san = board.san(mv)
    st["history"].append({"fen": t["fen"], "uci": mv.uci(), "san": user_san})
    board.push(mv)
    st["moves_made"] += 1
    res: dict = {"your_move": user_san}
    if assess.severity in ("mistake", "blunder"):
        res["coach"] = {"message": "The position just changed character.",
                        "question": "What does your opponent's best reply look like now, and what did your move stop guarding?"}
    if board.is_game_over() or st["moves_made"] >= st["max_moves"]:
        st["done"] = True
        ctx.st["sparring_last"] = {"fen": board.fen(), "user_color": st["user_color"]}
        res["phase_segment_complete"] = True
        return res
    # Opponent: Maia-style human reply at the growth-zone rating.
    opp_pred = await ctx.human.predict(board, st["opponent_elo"], ctx.profile.estimated_human_elo)
    rng = ctx.rng()
    top = opp_pred.top(4)
    moves, weights = zip(*top)
    reply = rng.choices(moves, weights=[w ** 2 for w in weights])[0]
    st["last_opponent_san"] = board.san(reply)
    res["opponent_move"] = st["last_opponent_san"]
    res["opponent_move_uci"] = reply.uci()
    board.push(reply)
    if board.is_game_over():
        st["done"] = True
        ctx.st["sparring_last"] = {"fen": board.fen(), "user_color": st["user_color"]}
        res["phase_segment_complete"] = True
        return res
    st["turn"] = await _new_turn(ctx, board)
    ctx.st["sparring_last"] = {"fen": board.fen(), "user_color": st["user_color"]}
    return res


async def act(ctx: Ctx, st: dict, action: dict) -> dict:
    if st["done"]:
        raise ApiError(409, "phase_done", "Sparring segment complete.")
    t = action["type"]
    if t == "sparring_plan":
        return await _plan(ctx, st, action)
    if t == "sparring_move":
        if st["turn"]["pm"] and not st["turn"]["pm"]["done"]:
            raise ApiError(409, "finish_post_mortem", "Finish the guided walkthrough first.")
        return await _move(ctx, st, action)
    if t == "post_mortem_move":
        pm = st["turn"]["pm"]
        if not pm:
            raise ApiError(409, "no_post_mortem", "No walkthrough in progress.")
        res = await post_mortem_move(ctx, pm, action["uci"])
        if pm["done"]:
            st["turn"]["unlocked"] = True
        return res
    raise ApiError(409, "bad_action", f"{t} is not valid in phase 3.")


async def hint(ctx: Ctx, st: dict, requested_tier: int | None, forfeit: bool) -> dict:
    t = st["turn"]
    board = chess.Board(t["fen"])
    return await give_hint(ctx, t, board, {"best_uci": t["best_uci"], "acceptable": t["acceptable"],
                                           "flawed_uci": t["trap_uci"], "user_strings": t["user_strings"]},
                           requested_tier, forfeit)


def current_board(st: dict) -> chess.Board | None:
    return None if st.get("done") else chess.Board(st["turn"]["fen"])


def solving(st: dict) -> dict | None:
    if st.get("done"):
        return None
    return {"best_uci": st["turn"]["best_uci"], "user_strings": st["turn"]["user_strings"]}
