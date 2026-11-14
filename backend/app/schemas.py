"""Request models. Responses are plain dicts built by the phase ``view``/``act``
functions and checked by ``api.leakguard`` before they leave the server."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

Uci = Annotated[str, Field(pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$")]
ThinkMs = Annotated[int | None, Field(default=None, ge=0, le=3_600_000)]


class CreateUser(BaseModel):
    display_name: str = Field("player", max_length=80)


class Settings(BaseModel):
    daily_budget_minutes: Literal[10, 20, 30]


class BlindfoldReady(BaseModel):
    type: Literal["blindfold_ready"]


class BlindfoldDictationDone(BaseModel):
    type: Literal["blindfold_dictation_done"]


class BlindfoldAnswer(BaseModel):
    type: Literal["blindfold_answer"]
    query_id: str = Field(max_length=8)
    answer: str = Field(max_length=40)
    think_ms: ThinkMs = None


class BlindfoldFinalMove(BaseModel):
    type: Literal["blindfold_final_move"]
    uci: Uci
    think_ms: ThinkMs = None


class PatternDiagnose(BaseModel):
    type: Literal["pattern_diagnose"]
    motif: str = Field(max_length=40)
    tempting_move: str = Field("", max_length=12)
    why: str = Field("", max_length=400)


class PatternMove(BaseModel):
    type: Literal["pattern_move"]
    uci: Uci
    think_ms: ThinkMs = None


class SparringPlan(BaseModel):
    """The mandatory 3-part think-aloud micro-plan."""

    type: Literal["sparring_plan"]
    candidates: list[Annotated[str, Field(max_length=12)]] = Field(min_length=1, max_length=3)
    chosen: str = Field("", max_length=12)
    opponent_reply: str = Field(max_length=200)
    purpose: str = Field(max_length=600)
    variation: list[Annotated[str, Field(max_length=12)]] = Field(default_factory=list, max_length=6)
    transcript: str = Field("", max_length=2000)


class SparringMove(BaseModel):
    type: Literal["sparring_move"]
    uci: Uci
    think_ms: ThinkMs = None


class PostMortemMove(BaseModel):
    type: Literal["post_mortem_move"]
    uci: Uci


class PerturbMove(BaseModel):
    type: Literal["perturb_move"]
    uci: Uci
    think_ms: ThinkMs = None


class RecallReconstruct(BaseModel):
    type: Literal["recall_reconstruct"]
    placement: str = Field(max_length=100)


class RecallReflect(BaseModel):
    type: Literal["recall_reflect"]
    key_moment: str = Field(max_length=2000)
    habit_tag: str = Field(max_length=200)


class IntegrityVerify(BaseModel):
    type: Literal["integrity_verify"]
    answers: dict[str, str]


class AdvancePhase(BaseModel):
    type: Literal["advance_phase"]


Action = Annotated[Union[
    BlindfoldReady, BlindfoldDictationDone, BlindfoldAnswer, BlindfoldFinalMove, PatternDiagnose, PatternMove,
    SparringPlan, SparringMove, PostMortemMove, PerturbMove, RecallReconstruct, RecallReflect, IntegrityVerify,
    AdvancePhase,
], Field(discriminator="type")]


class ActionEnvelope(BaseModel):
    action: Action


class HintRequest(BaseModel):
    requested_tier: int | None = Field(None, ge=1, le=4)
    forfeit: bool = False


class CoachMessage(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class PlanParse(BaseModel):
    transcript: str = Field(max_length=2000)
    fen: str = Field(max_length=100)


class PositionIn(BaseModel):
    fen: str = Field(max_length=100)
