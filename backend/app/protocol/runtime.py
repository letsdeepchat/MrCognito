"""Session runtime: phase sequencing, timers, integrity hold, coach chat, finalisation."""
from __future__ import annotations

from datetime import datetime, timedelta

import chess
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import integrity, telemetry as tm
from ..clock import as_utc
from ..coach.guardrails import detect_offloading, refusal_message
from ..coach.orchestrator import SocraticOrchestrator
from ..db import CognitiveProfile, DailyMetric, TrainingSession
from ..engines.base import ChessEngine
from ..engines.human_model import HumanMoveModel
from ..engines.tactics import hanging_pieces, most_tense_sector
from . import cooldown, p1_blindfold, p2_pattern, p3_sparring, p4_perturbation, p5_recall
from .common import ApiError, Ctx, coach_reply, masked_board, policy_sets, rag_for
from .phases import COGNITIVE_TARGET, ORDER, Phase, next_phase, phase_seconds

HANDLERS = {
    Phase.BLINDFOLD_WARMUP: p1_blindfold,
    Phase.PATTERN_INHIBITION: p2_pattern,
    Phase.SPARRING_VERBALIZED: p3_sparring,
    Phase.ADAPTABILITY_PERTURBATION: p4_perturbation,
    Phase.RECALL_CONSOLIDATION: p5_recall,
}


def phase_deadline(s: TrainingSession) -> datetime:
    d = as_utc(s.phase_started_at) + timedelta(seconds=phase_seconds(s.budget_minutes, Phase(s.current_phase)))
    return min(d, cooldown.hard_deadline(s))


async def load_active(db: AsyncSession, user_id: str) -> TrainingSession | None:
    return (await db.execute(select(TrainingSession).where(TrainingSession.user_id == user_id,
                                                           TrainingSession.state == "ACTIVE")
                             .order_by(TrainingSession.started_at.desc()).limit(1))).scalar_one_or_none()


async def start(db: AsyncSession, profile: CognitiveProfile, engine: ChessEngine, human: HumanMoveModel,
                coach: SocraticOrchestrator, now: datetime) -> Ctx:
    active = await load_active(db, profile.user_id)
    if active is not None and cooldown.expired(active, now):
        await finalize(Ctx(db, active, profile, engine, human, coach, now), "budget_reached")
        active = None
    cooldown.assert_can_start(profile, active, now)
    s = TrainingSession(user_id=profile.user_id, budget_minutes=profile.daily_budget_minutes, state="ACTIVE",
                        started_at=now, current_phase=Phase.BLINDFOLD_WARMUP.value, phase_started_at=now,
                        phase_state={"difficulty": profile.difficulty_rating, "scores": {}, "fatigue": [],
                                     "integrity": {"samples": [], "drill": None}, "phase_log": [],
                                     "yesterday_habit": profile.habit_for_tomorrow})
    db.add(s)
    await db.flush()
    ctx = Ctx(db, s, profile, engine, human, coach, now)
    await _enter(ctx, Phase.BLINDFOLD_WARMUP)
    return ctx


async def _enter(ctx: Ctx, phase: Phase, start_at: datetime | None = None) -> None:
    s = ctx.session
    s.current_phase = phase.value
    s.phase_started_at = start_at or ctx.now
    if phase is not Phase.BLINDFOLD_WARMUP:
        s.mask_level = 0
    s.phase_state["phase"] = {}
    s.phase_state["phase"] = await HANDLERS[phase].init(ctx)
    s.phase_state["phase_log"].append({"phase": phase.value, "started_at": as_utc(s.phase_started_at).isoformat()})
    ctx.touch()


async def tick(ctx: Ctx) -> None:
    """Lazy timer enforcement, run before and after every request on a session.

    A phase that ran out of time hands over at its *scheduled* deadline, so the
    phase clocks stay aligned with the session's hard deadline.
    """
    s = ctx.session
    if s.state != "ACTIVE":
        return
    if cooldown.expired(s, ctx.now):
        await finalize(ctx, "budget_reached")
        return
    for _ in range(len(ORDER)):
        phase = Phase(s.current_phase)
        done = s.phase_state.get("phase", {}).get("done")
        deadline = phase_deadline(s)
        if not done and ctx.now < deadline:
            break
        nxt = next_phase(phase)
        s.phase_state["phase_log"][-1]["ended_at"] = (ctx.now if done else deadline).isoformat()
        s.phase_state["phase_log"][-1]["reason"] = "completed" if done else "time"
        if nxt is None:
            await finalize(ctx, "protocol_complete" if done else "budget_reached")
            return
        await _enter(ctx, nxt, start_at=None if done else deadline)
    ctx.touch()


def _board_of(ctx: Ctx) -> chess.Board | None:
    return HANDLERS[Phase(ctx.session.current_phase)].current_board(ctx.ph)


def view(ctx: Ctx) -> dict:
    s = ctx.session
    out: dict = {"session_id": s.id, "state": s.state, "budget_minutes": s.budget_minutes,
                 "started_at": as_utc(s.started_at).isoformat(),
                 "hard_deadline": cooldown.hard_deadline(s).isoformat()}
    if s.state != "ACTIVE":
        out.update({"end_reason": s.end_reason, "ended_at": as_utc(s.ended_at).isoformat() if s.ended_at else None,
                    "next_unlock_at": as_utc(ctx.profile.next_unlock_at).isoformat() if ctx.profile.next_unlock_at else None,
                    "summary": s.summary})
        return out
    phase = Phase(s.current_phase)
    out.update({
        "phase": phase.value, "phase_index": ORDER.index(phase) + 1, "phase_count": len(ORDER),
        "cognitive_target": COGNITIVE_TARGET[phase], "phase_started_at": as_utc(s.phase_started_at).isoformat(),
        "phase_deadline": phase_deadline(s).isoformat(), "phase_seconds": phase_seconds(s.budget_minutes, phase),
        "server_time": ctx.iso(), "mask_level": s.mask_level,
        "integrity_hold": s.integrity_hold, "yesterday_habit": s.phase_state.get("yesterday_habit"),
        "fatigue_notice": s.phase_state.get("fatigue_notice"),
    })
    if s.integrity_hold:
        drill = s.phase_state["integrity"]["drill"]
        out["integrity_drill"] = {"instruction": "Input pacing looked unusual, so play is paused for a short blindfold "
                                                 "check of the current position.",
                                  "board": masked_board(drill["fen"], 3),
                                  "queries": p1_blindfold.public_queries(drill["queries"])}
        return out
    out["view"] = HANDLERS[phase].view(ctx, ctx.ph)
    return out


async def act(ctx: Ctx, action: dict) -> dict:
    s = ctx.session
    if s.state != "ACTIVE":
        raise ApiError(423, "session_closed", "This session is complete. Training unlocks after consolidation.")
    if s.integrity_hold and action["type"] != "integrity_verify":
        raise ApiError(423, "integrity_hold", "Complete the blindfold verification drill to resume.")
    if action["type"] == "integrity_verify":
        return await _verify(ctx, action)
    if action["type"] == "advance_phase":
        if not ctx.ph.get("done"):
            # Allowed: the user may move on early; unfinished items simply aren't scored.
            ctx.ph["done"] = True
        ctx.touch()
        return {"ok": True}
    phase = Phase(s.current_phase)
    res = await HANDLERS[phase].act(ctx, ctx.ph, action)
    await _integrity_check(ctx)
    ctx.touch()
    return res


async def hint(ctx: Ctx, requested_tier: int | None, forfeit: bool) -> dict:
    s = ctx.session
    if s.state != "ACTIVE" or s.integrity_hold:
        raise ApiError(423, "unavailable", "Hints are unavailable right now.")
    h = HANDLERS[Phase(s.current_phase)]
    if not hasattr(h, "hint"):
        raise ApiError(409, "no_hints_in_phase", "The hint ladder is used for puzzles and sparring.")
    res = await h.hint(ctx, ctx.ph, requested_tier, forfeit)
    ctx.touch()
    return res


async def coach_chat(ctx: Ctx, message: str) -> dict:
    board = _board_of(ctx)
    if detect_offloading(message):
        memory = Phase(ctx.session.current_phase) in (Phase.BLINDFOLD_WARMUP, Phase.RECALL_CONSOLIDATION)
        q = _diagnostic_question(None if memory else board)
        await ctx.event("offload_request_refused", {"text": message[:200]})
        return {"refused": True, "message": refusal_message(q), "question": q}
    if board is None or Phase(ctx.session.current_phase) in (Phase.BLINDFOLD_WARMUP, Phase.RECALL_CONSOLIDATION):
        # Memory drills: talking about the position would hand over the answer.
        return {"refused": False, "message": "This one is a memory drill, so the coach stays quiet about the board.",
                "question": "What do you notice first when you look at it?"}
    solving = HANDLERS[Phase(ctx.session.current_phase)].solving(ctx.ph) or {}
    allowed, forbidden = policy_sets(board, solving.get("best_uci"), solving.get("user_strings", []))
    reply = await coach_reply(ctx, board, {"kind": "chat", "user_text": message[:500],
                                           "rag": await rag_for(board)}, 0, allowed, forbidden)
    return {"refused": False, "message": reply.message, "question": reply.question, "source": reply.source}


def _diagnostic_question(board: chess.Board | None) -> str:
    if board is None:
        return "What is the single most important thing to remember about the position you just saw?"
    if board.is_check():
        return "You're in check. List every legal way out before choosing one."
    if hanging_pieces(board, board.turn):
        return "Which of your pieces is defended fewer times than it is attacked right now?"
    if hanging_pieces(board, not board.turn):
        return "Is every one of your opponent's pieces adequately defended? Check each one."
    return (f"What is your opponent threatening on the {most_tense_sector(board)}, and which of your pieces "
            "would be the first target?")


async def _integrity_check(ctx: Ctx) -> None:
    s = ctx.session
    samples = s.phase_state.setdefault("integrity", {}).setdefault("samples", [])
    v = integrity.evaluate(samples)
    if not v.suspicious or s.integrity_hold:
        return
    board = _board_of(ctx) or chess.Board()
    s.integrity_hold = True
    s.phase_state["integrity"]["pre_mask"] = s.mask_level
    s.mask_level = 3
    s.phase_state["integrity"]["drill"] = {"fen": board.fen(), "queries": p1_blindfold.make_queries(board, ctx.rng(), 3)}
    await ctx.event("integrity_hold", {"top1_rate": v.top1_rate, "latency_cv": v.latency_cv, "n": v.n})


async def _verify(ctx: Ctx, action: dict) -> dict:
    s = ctx.session
    if not s.integrity_hold:
        raise ApiError(409, "no_hold", "No verification pending.")
    drill = s.phase_state["integrity"]["drill"]
    answers = action.get("answers") or {}
    correct = sum(1 for q in drill["queries"] if p1_blindfold.check_answer(q, str(answers.get(q["id"], ""))))
    ctx.score("wm", correct / max(1, len(drill["queries"])))
    passed = correct >= 2
    await ctx.event("integrity_verify", {"correct": correct, "passed": passed})
    if passed:
        s.integrity_hold = False
        s.mask_level = s.phase_state["integrity"].get("pre_mask", 0)
        s.phase_state["integrity"] = {"samples": [], "drill": None}
    else:
        board = chess.Board(drill["fen"])
        s.phase_state["integrity"]["drill"] = {"fen": drill["fen"], "queries": p1_blindfold.make_queries(board, ctx.rng(), 3)}
    ctx.touch()
    return {"passed": passed, "correct": correct}


async def finalize(ctx: Ctx, reason: str) -> None:
    s, p = ctx.session, ctx.profile
    if s.state != "ACTIVE":
        return
    cooldown.complete(p, s, ctx.now, reason)
    sc = s.phase_state.get("scores", {})
    metrics = {
        "calculation_depth_index": tm.mean(sc.get("cdi", [])),
        "working_memory_score": tm.mean(sc.get("wm", [])),
        "trap_resistance_ratio": tm.mean(sc.get("trap", [])),
        "reasoning_alignment_score": tm.mean(sc.get("rma", [])),
        "adaptability_index": tm.mean(sc.get("adapt", [])),
        "success_rate": tm.mean(sc.get("outcomes", [])),
    }
    prior = (await ctx.db.execute(select(DailyMetric).where(DailyMetric.user_id == p.user_id))).scalars().all()
    first = len(prior) == 0
    for k in ("calculation_depth_index", "working_memory_score", "trap_resistance_ratio", "reasoning_alignment_score",
              "adaptability_index"):
        setattr(p, k, round(tm.ema(getattr(p, k) or 0.0, metrics[k], first or not getattr(p, k)), 4))
    p.difficulty_rating = float(s.phase_state.get("difficulty", p.difficulty_rating))
    if metrics["success_rate"] is not None:
        p.estimated_human_elo = int(round(0.85 * p.estimated_human_elo + 0.15 * p.difficulty_rating))
    ended = as_utc(s.ended_at)
    active_min = round((ended - as_utc(s.started_at)).total_seconds() / 60, 2)
    day = ended.date()
    row = next((r for r in prior if r.day == day), None)
    if row is None:
        row = DailyMetric(user_id=p.user_id, day=day, budget_minutes=s.budget_minutes, active_minutes=active_min)
        ctx.db.add(row)
    row.active_minutes = active_min
    for k, v in metrics.items():
        setattr(row, k, v)
    s.summary = {"metrics": metrics, "active_minutes": active_min, "end_reason": reason,
                 "fatigue_curve": tm.fatigue_curve(s.phase_state.get("fatigue", [])),
                 "phases": s.phase_state.get("phase_log", []), "habit_for_tomorrow": p.habit_for_tomorrow}
    s.integrity_hold = False
    ctx.touch()
