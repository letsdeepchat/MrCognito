"""Deterministic JSON tool layer between the LLM and the chess state engine.

Every tool returns *categorical* facts. No tool output contains an engine
move or a centipawn number, so even a compromised/jail-broken LLM has nothing
to leak. Inputs are validated against strict JSON Schemas before execution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import chess
from jsonschema import Draft202012Validator

from ..engines.base import ChessEngine
from ..engines.tactics import assess_move, null_move_threat, sector_tension

SAN_LIST = {"type": "array", "items": {"type": "string", "maxLength": 12}, "maxItems": 8}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "validate_user_hypothesis",
        "description": "Check the user's own stated candidate moves, predicted opponent reply, or claimed threat against "
                       "ground truth. Returns categorical verdicts only (holds / dubious / fails, motif tags).",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["hypothesis_type", "moves", "after_move"],
            "properties": {
                "hypothesis_type": {"type": "string", "enum": ["candidate_move", "opponent_reply", "threat"]},
                "moves": SAN_LIST,
                "after_move": {"type": "string", "maxLength": 12,
                               "description": "For opponent_reply: the user's candidate the reply answers; else empty."},
            },
        },
    },
    {
        "name": "get_board_sector_tension",
        "description": "Counts of contested squares, pressure balance and insufficiently defended pieces in a sector "
                       "(queenside|center|kingside|<file>-file|rank-<n>|<sq>-<sq> diagonal).",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["sector"],
                         "properties": {"sector": {"type": "string", "maxLength": 12}}},
    },
    {
        "name": "evaluate_calculation_depth",
        "description": "Given a variation the user articulated (SAN, alternating sides from the current position), "
                       "return how many plies are accurate before the first error.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["variation"],
                         "properties": {"variation": SAN_LIST}},
    },
    {
        "name": "trigger_blindfold_mask",
        "description": "Raise the board-masking level (0 full, 1 silhouettes, 2 coordinates only, 3 empty) to force "
                       "visualisation.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["level", "reason"],
                         "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 3},
                                        "reason": {"type": "string", "maxLength": 200}}},
    },
    {
        "name": "log_cognitive_telemetry",
        "description": "Record an observation about the user's thinking for the longitudinal profile.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["event", "detail"],
                         "properties": {"event": {"type": "string", "enum": [
                             "overlooked_forcing_reply", "shallow_calculation", "good_prophylaxis", "impulsive_move",
                             "clear_articulation", "vague_plan", "fatigue_signal"]},
                             "detail": {"type": "string", "maxLength": 300}}},
    },
]

REPLY_SCHEMA = {
    "name": "socratic_reply",
    "description": "Your final answer to the user. Must be Socratic: guide with a question, never state a move, "
                   "score, or who is better.",
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["message", "question", "concepts"],
        "properties": {
            "message": {"type": "string", "maxLength": 600},
            "question": {"type": "string", "maxLength": 300},
            "concepts": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 4},
        },
    },
}

ALL_TOOLS = TOOL_SCHEMAS + [REPLY_SCHEMA]
VALIDATORS = {t["name"]: Draft202012Validator(t["input_schema"]) for t in ALL_TOOLS}


class ToolValidationError(ValueError):
    pass


def validate_args(name: str, args: Any) -> dict:
    if name not in VALIDATORS:
        raise ToolValidationError(f"unknown tool {name!r}")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(f"invalid JSON: {exc}") from exc
    errors = sorted(VALIDATORS[name].iter_errors(args), key=lambda e: e.path)
    if errors:
        raise ToolValidationError("; ".join(e.message for e in errors[:3]))
    return args


def parse_san(board: chess.Board, san: str) -> chess.Move | None:
    try:
        return board.parse_san(san.strip().replace("0-0-0", "O-O-O").replace("0-0", "O-O"))
    except (ValueError, AssertionError):
        try:
            mv = chess.Move.from_uci(san.strip())
            return mv if mv in board.legal_moves else None
        except ValueError:
            return None


@dataclass
class ToolContext:
    board: chess.Board
    engine: ChessEngine
    depth: int = 12
    on_mask: Callable[[int, str], Awaitable[None]] | None = None
    on_log: Callable[[str, dict], Awaitable[None]] | None = None
    calls: list[dict] = field(default_factory=list)


async def _validate_hypothesis(ctx: ToolContext, hypothesis_type: str, moves: list[str], after_move: str) -> dict:
    out: list[dict] = []
    if hypothesis_type == "candidate_move":
        for san in moves[:3]:
            mv = parse_san(ctx.board, san)
            if mv is None:
                out.append({"move": san, "legal": False})
                continue
            a = await assess_move(ctx.engine, ctx.board, mv, depth=ctx.depth)
            nb = ctx.board.copy(stack=False)
            nb.push(mv)
            forcing = bool(a.refutation) and (nb.gives_check(a.refutation[0]) or nb.is_capture(a.refutation[0]))
            out.append({"move": san, "legal": True,
                        "soundness": "holds" if a.delta_cp < 60 else "dubious" if a.delta_cp < 150 else "fails",
                        "fails_to_forcing_reply": a.delta_cp >= 150 and forcing,
                        "refutation_motif": a.refutation_motif, "flags": a.flags})
        return {"hypothesis_type": hypothesis_type, "verdicts": out}
    if hypothesis_type == "opponent_reply":
        cand = parse_san(ctx.board, after_move) if after_move else None
        if cand is None:
            return {"hypothesis_type": hypothesis_type, "error": "after_move must be a legal candidate"}
        nb = ctx.board.copy(stack=False)
        nb.push(cand)
        an = await ctx.engine.analyse(nb, depth=ctx.depth, multipv=3)
        best = an.best.cp if an.best else 0
        for san in moves[:2]:
            mv = parse_san(nb, san)
            cp = an.cp_of(mv) if mv else None
            strongest = cp is not None and best - cp <= 60
            forcing_exists = bool(an.best) and (nb.gives_check(an.best.move) or nb.is_capture(an.best.move))
            out.append({"move": san, "legal": mv is not None, "is_among_strongest_replies": strongest,
                        "a_forcing_reply_exists": forcing_exists})
        return {"hypothesis_type": hypothesis_type, "verdicts": out}
    # threat: is the opponent's claimed idea a real threat if we passed?
    threat_gain = await null_move_threat(ctx.engine, ctx.board, depth=max(6, ctx.depth - 2))
    nb = ctx.board.copy(stack=False)
    nb.push(chess.Move.null())
    nb = chess.Board(nb.fen())
    for san in moves[:2]:
        mv = parse_san(nb, san)
        real = False
        if mv is not None:
            a = await assess_move(ctx.engine, nb, mv, depth=max(6, ctx.depth - 2))
            real = a.delta_cp < 80 and threat_gain >= 100
        out.append({"move": san, "legal": mv is not None, "is_real_threat": real})
    return {"hypothesis_type": hypothesis_type, "opponent_has_serious_threat": threat_gain >= 150, "verdicts": out}


async def calculation_depth(board: chess.Board, engine: ChessEngine, variation: list[str], depth: int = 12,
                            tolerance: int = 80) -> dict:
    b = board.copy(stack=False)
    accurate = 0
    for i, san in enumerate(variation[:8]):
        mv = parse_san(b, san)
        if mv is None:
            return {"accurate_plies": accurate, "total_plies": len(variation), "first_error_ply": i + 1, "legal": False}
        an = await engine.analyse(b, depth=depth, multipv=4)
        cp = an.cp_of(mv)
        if cp is None:  # not in top lines: measure directly
            nb = b.copy(stack=False)
            nb.push(mv)
            after = await engine.analyse(nb, depth=depth)
            cp = -(after.best.cp if after.best else 0)
        if an.best and an.best.cp - cp > tolerance:
            return {"accurate_plies": accurate, "total_plies": len(variation), "first_error_ply": i + 1, "legal": True}
        accurate += 1
        b.push(mv)
    return {"accurate_plies": accurate, "total_plies": len(variation), "first_error_ply": None, "legal": True}


async def execute_tool(ctx: ToolContext, name: str, raw_args: Any) -> dict:
    args = validate_args(name, raw_args)
    ctx.calls.append({"name": name, "args": args})
    if name == "validate_user_hypothesis":
        return await _validate_hypothesis(ctx, **args)
    if name == "get_board_sector_tension":
        try:
            return sector_tension(ctx.board, args["sector"])
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "evaluate_calculation_depth":
        return await calculation_depth(ctx.board, ctx.engine, args["variation"], ctx.depth)
    if name == "trigger_blindfold_mask":
        if ctx.on_mask:
            await ctx.on_mask(args["level"], args["reason"])
        return {"ok": True, "level": args["level"]}
    if name == "log_cognitive_telemetry":
        if ctx.on_log:
            await ctx.on_log(args["event"], {"detail": args["detail"]})
        return {"ok": True}
    raise ToolValidationError(f"{name} is not executable")
