"""Human-behavioural move models (rating-calibrated move distributions).

* ``Maia2Model`` wraps the real Maia-2 network (pip ``maia2``; weights are
  fetched by the package from Google Drive on first use, needs torch).
* ``HeuristicHumanModel`` is a dependency-free approximation: a softmax over
  engine MultiPV scores blended with "salience" priors (captures, checks,
  attacks on big pieces, recaptures) whose weight decays with rating. It is
  deliberately biased the way club players are, which is exactly what trap
  detection needs. Swap in Maia-2 via ``COGNITO_HUMAN_MODEL=maia2``.

A Chessformer adapter can implement the same ``HumanMoveModel`` protocol.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Protocol

import chess

from .base import MATE_CP, ChessEngine
from .material import VALUES

log = logging.getLogger(__name__)


@dataclass
class HumanPrediction:
    probs: dict[chess.Move, float]  # sums to ~1 over legal moves

    def top(self, k: int = 3) -> list[tuple[chess.Move, float]]:
        return sorted(self.probs.items(), key=lambda kv: -kv[1])[:k]

    def prob(self, move: chess.Move) -> float:
        return self.probs.get(move, 0.0)


class HumanMoveModel(Protocol):
    name: str

    async def predict(self, board: chess.Board, elo_self: int, elo_oppo: int) -> HumanPrediction: ...


def salience(board: chess.Board, move: chess.Move) -> float:
    """How 'eye-catching' a move is to a human, independent of soundness."""
    s = 0.0
    mover = board.piece_type_at(move.from_square) or chess.PAWN
    if board.is_capture(move):
        victim = board.piece_type_at(move.to_square) or chess.PAWN
        s += 1.0 + VALUES[victim] / 300
        last = board.peek() if board.move_stack else None
        if last is not None and last.to_square == move.to_square:
            s += 1.2  # recapture reflex
    if board.gives_check(move):
        s += 1.1
    if move.promotion:
        s += 2.0
    b = board.copy(stack=False)
    b.push(move)
    attacked = [sq for sq in b.attacks(move.to_square) if (p := b.piece_at(sq)) and p.color != board.turn]
    big = [sq for sq in attacked if (b.piece_type_at(sq) or 0) in (chess.QUEEN, chess.ROOK, chess.KING)]
    s += 0.5 * len(big) + (0.6 if len(attacked) >= 2 else 0.0)
    if mover in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) in (0, 7):
        s += 0.4  # development
    if mover == chess.KING and not board.is_castling(move):
        s -= 0.6
    if board.is_castling(move):
        s += 0.5
    return s


class HeuristicHumanModel:
    name = "heuristic-human"

    def __init__(self, engine: ChessEngine, depth: int = 8) -> None:
        self.engine = engine
        self.depth = depth

    async def predict(self, board: chess.Board, elo_self: int, elo_oppo: int = 1500) -> HumanPrediction:
        legal = list(board.legal_moves)
        if not legal:
            return HumanPrediction({})
        an = await self.engine.analyse(board, depth=self.depth, multipv=len(legal))
        cps = {ln.move: ln.cp for ln in an.lines}
        floor = min(cps.values(), default=0) - 50
        # Stronger players track evaluation; weaker players track salience.
        t = (min(max(elo_self, 600), 2600) - 600) / 2000  # 0..1
        w_eval = 0.4 + 2.6 * t  # per 100cp
        w_sal = 1.4 - 1.0 * t
        logits: dict[chess.Move, float] = {}
        for m in legal:
            cp = max(-MATE_CP // 10, min(MATE_CP // 10, cps.get(m, floor)))
            logits[m] = w_eval * cp / 100 + w_sal * salience(board, m)
        mx = max(logits.values())
        exps = {m: math.exp(v - mx) for m, v in logits.items()}
        z = sum(exps.values())
        return HumanPrediction({m: v / z for m, v in exps.items()})


class Maia2Model:  # pragma: no cover - needs torch + downloaded weights
    name = "maia2"

    def __init__(self, model_type: str = "rapid", save_root: str = "./maia2_models", device: str = "cpu") -> None:
        from maia2 import inference, model  # type: ignore

        self._inference = inference
        self._model = model.from_pretrained(type=model_type, device=device, save_root=save_root)
        self._prepared = inference.prepare()

    def _predict_sync(self, fen: str, elo_self: int, elo_oppo: int) -> HumanPrediction:
        move_probs, _win_prob = self._inference.inference_each(self._model, self._prepared, fen, elo_self, elo_oppo)
        return HumanPrediction({chess.Move.from_uci(u): float(p) for u, p in move_probs.items()})

    async def predict(self, board: chess.Board, elo_self: int, elo_oppo: int = 1500) -> HumanPrediction:
        return await asyncio.to_thread(self._predict_sync, board.fen(), elo_self, elo_oppo)


def build_human_model(kind: str, engine: ChessEngine, **kw) -> HumanMoveModel:
    if kind == "maia2":
        try:
            return Maia2Model(kw.get("model_type", "rapid"), kw.get("save_root", "./maia2_models"))
        except Exception as exc:  # pragma: no cover
            log.warning("Maia-2 unavailable (%s); falling back to heuristic human model", exc)
    return HeuristicHumanModel(engine)
