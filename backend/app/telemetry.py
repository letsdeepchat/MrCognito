"""Cognitive metrics: CDI, WMRS, TRR, RMA, adaptability, fatigue and adaptive difficulty."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import chess

TARGET_SUCCESS = 0.72  # optimal challenge zone ~70-75 %
STAIRCASE_K = 40.0
EMA_ALPHA = 0.3


def staircase(rating: float, success: float) -> float:
    """Adaptive difficulty: move toward ~72 % success."""
    return max(600.0, min(2600.0, rating + STAIRCASE_K * (success - TARGET_SUCCESS) * 2))


def ema(prev: float, new: float | None, first: bool) -> float:
    if new is None:
        return prev
    return new if first else (1 - EMA_ALPHA) * prev + EMA_ALPHA * new


def mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


# --- Reasoning-to-Move Alignment -------------------------------------------
PURPOSE_CONCEPTS = {
    "king_attack": r"\b(attack|mate|king|checkmate|storm)\b",
    "defence": r"\b(defend|defen[cs]e|protect|guard|cover|safety|safe)\b",
    "centre": r"\b(cent(er|re)|central|middle)\b",
    "activity": r"\b(activat|develop|improv|coordinat|active|mobil)\w*",
    "restriction": r"\b(restrict|prevent|stop|block|deny|limit|prophyla)\w*",
    "trade": r"\b(trade|exchange|swap|simplif)\w*",
    "material": r"\b(win|capture|take|grab|pawn up|material)\b",
    "lines": r"\b(open|file|diagonal|line|rank)\b",
}


def purpose_concepts(text: str) -> set[str]:
    t = (text or "").lower()
    return {k for k, rx in PURPOSE_CONCEPTS.items() if re.search(rx, t)}


def move_consequences(board: chess.Board, move: chess.Move) -> set[str]:
    """What the executed move actually does, in the same concept vocabulary."""
    out: set[str] = set()
    me = board.turn
    after = board.copy(stack=False)
    after.push(move)
    if board.is_capture(move):
        out |= {"material", "trade"}
    if after.is_check():
        out.add("king_attack")
    enemy_king = board.king(not me)
    if enemy_king is not None and chess.square_distance(move.to_square, enemy_king) <= 2:
        out.add("king_attack")
    if move.to_square in (chess.D4, chess.E4, chess.D5, chess.E5) or chess.square_file(move.to_square) in (3, 4):
        out.add("centre")
    mob_before = sum(1 for m in board.legal_moves)
    nb = after.copy(stack=False)
    nb.push(chess.Move.null())
    mob_after = sum(1 for m in nb.legal_moves) if nb.is_valid() else mob_before
    if mob_after > mob_before:
        out.add("activity")
    opp_before = board.copy(stack=False)
    opp_before.push(chess.Move.null())
    opp_mob_before = opp_before.legal_moves.count() if opp_before.is_valid() else 0
    if after.legal_moves.count() < opp_mob_before:
        out.add("restriction")
    own_king = board.king(me)
    if own_king is not None and (board.is_castling(move) or chess.square_distance(move.to_square, own_king) <= 1):
        out.add("defence")
    if board.piece_type_at(move.from_square) in (chess.ROOK, chess.QUEEN, chess.BISHOP):
        out.add("lines")
    return out


def reasoning_alignment(board: chess.Board, move: chess.Move, candidates: list[chess.Move], predicted_reply: str,
                        engine_reply: chess.Move | None, purpose: str) -> float:
    """RMA in [0, 1]: plan->move consistency, reply foresight, purpose->consequence overlap."""
    in_cands = 1.0 if move in candidates else 0.0
    reply_score = 0.0
    if engine_reply is not None:
        after = board.copy(stack=False)
        after.push(move)
        try:
            pr = after.parse_san(predicted_reply.strip()) if predicted_reply else None
        except ValueError:
            pr = None
        if pr == engine_reply:
            reply_score = 1.0
        elif pr is not None and (after.is_capture(pr) == after.is_capture(engine_reply)) and \
                (after.gives_check(pr) == after.gives_check(engine_reply)):
            reply_score = 0.5
        elif (after.gives_check(engine_reply) and "check" in (predicted_reply or "").lower()) or \
                (after.is_capture(engine_reply) and re.search(r"take|captur|x", predicted_reply or "", re.I)):
            reply_score = 0.5
    stated = purpose_concepts(purpose)
    actual = move_consequences(board, move)
    overlap = len(stated & actual) / len(stated) if stated else 0.0
    return round(0.4 * in_cands + 0.3 * reply_score + 0.3 * overlap, 3)


# --- Fatigue ---------------------------------------------------------------
@dataclass
class FatigueSignal:
    fatigued: bool
    latency_ratio: float
    recent_accuracy: float | None


def fatigue(samples: list[list]) -> FatigueSignal:
    """samples: [t_offset_s, latency_ms, correct(0/1)]. Compares the latest window to the session baseline."""
    if len(samples) < 6:
        return FatigueSignal(False, 1.0, None)
    base = [s[1] for s in samples[:3]]
    recent = samples[-3:]
    ratio = statistics.mean(s[1] for s in recent) / max(1.0, statistics.mean(base))
    acc = statistics.mean(s[2] for s in recent)
    return FatigueSignal(ratio >= 1.8 and acc < 0.5, round(ratio, 2), round(acc, 2))


def fatigue_curve(samples: list[list], bucket_s: int = 60) -> list[dict]:
    buckets: dict[int, list] = {}
    for t, lat, ok in samples:
        buckets.setdefault(int(t // bucket_s), []).append((lat, ok))
    return [{"minute": k, "mean_latency_ms": int(statistics.mean(x[0] for x in v)),
             "accuracy": round(statistics.mean(x[1] for x in v), 2)} for k, v in sorted(buckets.items())]
