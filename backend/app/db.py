"""Async SQLAlchemy models. PostgreSQL in production, SQLite for tests/dev.

Engine-derived data (best moves, PVs, evaluations) lives ONLY in
``TrainingSession.phase_state`` (server-private working memory) and in
``TurnTelemetry`` (analytics, never served raw during an active phase).
"""
from __future__ import annotations

import logging

import uuid
from datetime import date, datetime
from typing import Any, AsyncIterator

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .clock import utcnow
from .config import get_settings


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: MutableDict.as_mutable(JSON)}


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    display_name: Mapped[str] = mapped_column(String(80), default="player")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CognitiveProfile(Base):
    """Mirrors the ``UserCognitiveProfile`` reference model."""

    __tablename__ = "cognitive_profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    daily_budget_minutes: Mapped[int] = mapped_column(Integer, default=20)
    last_session_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_unlock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_human_elo: Mapped[int] = mapped_column(Integer, default=1200)
    # Adaptive-difficulty staircase rating (puzzle / opponent selection).
    difficulty_rating: Mapped[float] = mapped_column(Float, default=1200.0)
    calculation_depth_index: Mapped[float] = mapped_column(Float, default=0.0)
    working_memory_score: Mapped[float] = mapped_column(Float, default=0.0)
    trap_resistance_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning_alignment_score: Mapped[float] = mapped_column(Float, default=0.0)
    adaptability_index: Mapped[float] = mapped_column(Float, default=0.0)
    wellness_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    habit_for_tomorrow: Mapped[str | None] = mapped_column(String(200), nullable=True)


class TrainingSession(Base):
    __tablename__ = "training_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    budget_minutes: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | COMPLETED
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_phase: Mapped[str] = mapped_column(String(40))
    phase_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mask_level: Mapped[int] = mapped_column(Integer, default=0)
    integrity_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    # Server-private working state (answers, engine lines, hint ladders). NEVER serialised directly.
    phase_state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)


class TurnTelemetry(Base):
    """Mirrors the ``SessionTurnTelemetry`` reference model (plus scoring extras)."""

    __tablename__ = "turn_telemetry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True)
    phase: Mapped[str] = mapped_column(String(40))
    item_kind: Mapped[str] = mapped_column(String(40))
    fen: Mapped[str] = mapped_column(String(100))
    user_verbalized_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_move_uci: Mapped[str | None] = mapped_column(String(8), nullable=True)
    stockfish_eval_delta_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maia_human_move_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    hint_tier_used: Mapped[int] = mapped_column(Integer, default=0)
    think_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    matched_engine_top1: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CognitiveEvent(Base):
    """Free-form events written by the ``log_cognitive_telemetry`` tool and the protocol."""

    __tablename__ = "cognitive_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    budget_minutes: Mapped[int] = mapped_column(Integer)
    active_minutes: Mapped[float] = mapped_column(Float)
    calculation_depth_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    working_memory_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    trap_resistance_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_alignment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    adaptability_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)


class UserPosition(Base):
    """Positions the user digitised (camera / manual FEN) to study in a future sparring phase."""

    __tablename__ = "user_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fen: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(16))  # camera | manual
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


log = logging.getLogger(__name__)
_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> None:
    global _engine, _sessionmaker
    _engine = create_async_engine(url or get_settings().database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


vector_available = False


async def create_all() -> None:
    global vector_available
    assert _engine is not None
    if _engine.dialect.name == "postgresql":
        from sqlalchemy import text

        try:  # managed Postgres may not ship pgvector; the RAG store then falls back to memory
            async with _engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            vector_available = True
        except Exception as e:  # noqa: BLE001
            log.warning("pgvector unavailable (%s); using the in-memory RAG store", e)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


async def get_db() -> AsyncIterator[AsyncSession]:
    assert _sessionmaker is not None, "init_engine() not called"
    async with _sessionmaker() as s:
        yield s


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    assert _sessionmaker is not None
    return _sessionmaker
