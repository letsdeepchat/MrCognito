"""HTTP API."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import chess
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import as_utc, utcnow
from ..coach.orchestrator import SocraticOrchestrator
from ..coach.plan_parser import parse_plan
from ..db import CognitiveProfile, DailyMetric, TrainingSession, User, UserPosition, get_db
from ..engines.registry import get_engine, get_human_model
from ..protocol import cooldown, runtime
from ..protocol.common import ApiError, Ctx
from .. import schemas
from ..vision.recognizer import get_recognizer
from .leakguard import LeakGuardRoute

router = APIRouter(prefix="/api", route_class=LeakGuardRoute)

_coach: SocraticOrchestrator | None = None

WELLNESS_NOTICE = (
    "Sustained, effortful mental exercise supports cognitive reserve and executive function. This application is an "
    "educational cognitive-training tool. It does not diagnose, treat, cure, or prevent Alzheimer's disease or any "
    "other medical condition. If you have concerns about memory or cognition, please talk to a healthcare professional."
)


def get_coach() -> SocraticOrchestrator:
    global _coach
    if _coach is None:
        _coach = SocraticOrchestrator()
    return _coach


def set_coach(c: SocraticOrchestrator | None) -> None:
    global _coach
    _coach = c


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user(authorization: str = Header(""), db: AsyncSession = Depends(get_db)) -> tuple[User, CognitiveProfile]:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    user = (await db.execute(select(User).where(User.token_hash == _hash(token)))).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "invalid token")
    prof = await db.get(CognitiveProfile, user.id)
    return user, prof


def _err(e: ApiError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": e.message, **e.extra})


def _gate_err(e: cooldown.GateError) -> HTTPException:
    status = 423 if e.code == "consolidating" else 409 if e.code == "session_active" else 422
    return HTTPException(status, {"code": e.code, "message": e.message,
                                  "next_unlock_at": e.unlock_at.isoformat() if e.unlock_at else None})


def _ctx(db, session, prof) -> Ctx:
    return Ctx(db, session, prof, get_engine(), get_human_model(), get_coach(), utcnow())


async def _profile_view(db: AsyncSession, prof: CognitiveProfile) -> dict:
    now = utcnow()
    active = await runtime.load_active(db, prof.user_id)
    if active is not None and cooldown.expired(active, now):
        await runtime.finalize(_ctx(db, active, prof), "budget_reached")
        await db.commit()
        active = None
    gate = cooldown.gate_state(prof, active, now)
    return {
        "user_id": prof.user_id, "gate": gate.value, "daily_budget_minutes": prof.daily_budget_minutes,
        "last_session_completed_at": as_utc(prof.last_session_completed_at).isoformat() if prof.last_session_completed_at else None,
        "next_unlock_at": as_utc(prof.next_unlock_at).isoformat() if prof.next_unlock_at else None,
        "active_session_id": active.id if active is not None and gate.value == "ACTIVE" else None,
        "estimated_human_elo": prof.estimated_human_elo,
        "cognitive_indices": {
            "calculation_depth_index": prof.calculation_depth_index, "working_memory_score": prof.working_memory_score,
            "trap_resistance_ratio": prof.trap_resistance_ratio, "reasoning_alignment_score": prof.reasoning_alignment_score,
            "adaptability_index": prof.adaptability_index},
        "habit_for_tomorrow": prof.habit_for_tomorrow,
        "wellness_acknowledged": prof.wellness_acknowledged_at is not None,
        "wellness_notice": WELLNESS_NOTICE,
        "server_time": now.isoformat(),
    }


@router.get("/health")
async def health() -> dict:
    eng = get_engine()
    return {"ok": True, "engine": getattr(eng, "version", eng.name), "human_model": get_human_model().name,
            "coach": get_coach().llm.name}


@router.get("/wellness")
async def wellness() -> dict:
    return {"notice": WELLNESS_NOTICE}


@router.post("/users")
async def create_user(body: schemas.CreateUser, db: AsyncSession = Depends(get_db)) -> dict:
    token = secrets.token_urlsafe(32)
    user = User(display_name=body.display_name, token_hash=_hash(token))
    db.add(user)
    await db.flush()
    db.add(CognitiveProfile(user_id=user.id))
    await db.commit()
    return {"user_id": user.id, "token": token}


@router.get("/me")
async def me(u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await _profile_view(db, u[1])


@router.put("/me/settings")
async def update_settings(body: schemas.Settings, u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    prof = u[1]
    active = await runtime.load_active(db, prof.user_id)
    if active is not None and not cooldown.expired(active, utcnow()):
        raise HTTPException(409, {"code": "session_active", "message": "Change the budget between sessions."})
    try:
        cooldown.validate_budget(body.daily_budget_minutes)
    except cooldown.GateError as e:
        raise _gate_err(e)
    prof.daily_budget_minutes = body.daily_budget_minutes  # never touches next_unlock_at
    await db.commit()
    return await _profile_view(db, prof)


@router.post("/me/wellness-ack")
async def wellness_ack(u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    u[1].wellness_acknowledged_at = utcnow()
    await db.commit()
    return await _profile_view(db, u[1])


@router.post("/sessions")
async def start_session(u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    prof = u[1]
    if prof.wellness_acknowledged_at is None:
        raise HTTPException(428, {"code": "wellness_ack_required", "message": "Please read the wellness notice first.",
                                  "notice": WELLNESS_NOTICE})
    try:
        ctx = await runtime.start(db, prof, get_engine(), get_human_model(), get_coach(), utcnow())
    except cooldown.GateError as e:
        raise _gate_err(e)
    await db.commit()
    return runtime.view(ctx)


async def _load(db: AsyncSession, prof: CognitiveProfile, session_id: str) -> Ctx:
    s = await db.get(TrainingSession, session_id)
    if s is None or s.user_id != prof.user_id:
        raise HTTPException(404, "session not found")
    ctx = _ctx(db, s, prof)
    await runtime.tick(ctx)
    return ctx


@router.get("/sessions/current")
async def current_session(u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    prof = u[1]
    s = await runtime.load_active(db, prof.user_id)
    if s is None:
        last = (await db.execute(select(TrainingSession).where(TrainingSession.user_id == prof.user_id)
                                 .order_by(TrainingSession.started_at.desc()).limit(1))).scalar_one_or_none()
        if last is None:
            raise HTTPException(404, {"code": "no_session", "message": "No session yet."})
        return runtime.view(_ctx(db, last, prof))
    ctx = _ctx(db, s, prof)
    await runtime.tick(ctx)
    await db.commit()
    return runtime.view(ctx)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    ctx = await _load(db, u[1], session_id)
    await db.commit()
    return runtime.view(ctx)


@router.post("/sessions/{session_id}/action")
async def session_action(session_id: str, body: schemas.ActionEnvelope, u=Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    ctx = await _load(db, u[1], session_id)
    try:
        result = await runtime.act(ctx, body.action.model_dump())
    except ApiError as e:
        await db.commit()
        raise _err(e)
    await runtime.tick(ctx)
    await db.commit()
    return {"result": result, "session": runtime.view(ctx)}


@router.post("/sessions/{session_id}/hint")
async def session_hint(session_id: str, body: schemas.HintRequest, u=Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    ctx = await _load(db, u[1], session_id)
    if ctx.session.state != "ACTIVE":
        await db.commit()
        raise HTTPException(423, {"code": "session_closed", "message": "Session complete."})
    try:
        hint = await runtime.hint(ctx, body.requested_tier, body.forfeit)
    except ApiError as e:
        raise _err(e)
    await db.commit()
    return {"hint": hint, "session": runtime.view(ctx)}


@router.post("/sessions/{session_id}/coach")
async def session_coach(session_id: str, body: schemas.CoachMessage, u=Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> dict:
    ctx = await _load(db, u[1], session_id)
    if ctx.session.state != "ACTIVE":
        await db.commit()
        raise HTTPException(423, {"code": "session_closed", "message": "Session complete."})
    out = await runtime.coach_chat(ctx, body.message)
    await db.commit()
    return out


@router.post("/plan/parse")
async def plan_parse(body: schemas.PlanParse, u=Depends(current_user)) -> dict:
    try:
        return parse_plan(body.fen, body.transcript)
    except ValueError:
        raise HTTPException(422, "invalid fen")


@router.get("/dashboard")
async def dashboard(days: int = 30, u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    days = max(1, min(days, 90))
    since = (utcnow() - timedelta(days=days - 1)).date()
    rows = (await db.execute(select(DailyMetric).where(DailyMetric.user_id == u[1].user_id, DailyMetric.day >= since)
                             .order_by(DailyMetric.day))).scalars().all()
    by_day = {r.day: r for r in rows}
    series = []
    for i in range(days):
        d = since + timedelta(days=i)
        r = by_day.get(d)
        series.append({
            "day": d.isoformat(),
            "working_memory": r.working_memory_score if r else None,
            "calculation_depth": r.calculation_depth_index if r else None,
            "trap_resistance": r.trap_resistance_ratio if r else None,
            "reasoning_alignment": r.reasoning_alignment_score if r else None,
            "adaptability": r.adaptability_index if r else None,
            "active_minutes": r.active_minutes if r else 0,
            "budget_minutes": r.budget_minutes if r else None,
            "adherent": bool(r and r.active_minutes >= 5),
        })
    trained = [s for s in series if s["budget_minutes"]]
    return {"days": days, "series": series,
            "consistency": {"days_trained": len(trained), "adherence_rate": round(sum(s["adherent"] for s in series) / days, 3)}}


@router.post("/positions")
async def add_position(body: schemas.PositionIn, u=Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    try:
        b = chess.Board(body.fen)
    except ValueError:
        raise HTTPException(422, {"code": "invalid_fen", "message": "Not a parseable FEN."})
    if not b.is_valid() or b.is_game_over():
        raise HTTPException(422, {"code": "illegal_position", "message": "That position is not legal or is already over."})
    db.add(UserPosition(user_id=u[1].user_id, fen=b.fen(), source="manual"))
    await db.commit()
    return {"saved": True, "fen": b.fen(), "note": "It will be your sparring position in your next session."}


@router.post("/vision/board-to-fen")
async def board_to_fen(image: UploadFile = File(...), side_to_move: str = "w", u=Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    rec = get_recognizer()
    if rec is None:
        raise HTTPException(503, {"code": "vision_unavailable",
                                  "message": "No board-recognition model configured (COGNITO_VISION_ONNX_PATH)."})
    data = await image.read()
    if len(data) > 8_000_000:
        raise HTTPException(413, "image too large")
    result = await rec.recognize(data, side_to_move)
    if result.valid:
        db.add(UserPosition(user_id=u[1].user_id, fen=result.fen, source="camera"))
        await db.commit()
    return {"fen": result.fen, "valid": result.valid, "confidence": result.confidence, "issues": result.issues}
