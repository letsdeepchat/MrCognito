"""Pure-Python fallback engine (material + mobility, alpha-beta with quiescence).

Used when no UCI binary is installed so the stack and the test-suite still run
hermetically. It is weak but tactically sane at shallow depth (sees hanging
pieces, simple forks, mate-in-one).
"""
from __future__ import annotations

import asyncio

import chess

from .base import MATE_CP, Analysis, EngineLine

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
CENTER = {chess.D4, chess.E4, chess.D5, chess.E5}


def static_eval(board: chess.Board) -> int:
    """Score from side-to-move POV."""
    if board.is_checkmate():
        return -MATE_CP
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    s = 0
    for sq, pc in board.piece_map().items():
        v = VALUES[pc.piece_type]
        if pc.piece_type == chess.PAWN:
            rank = chess.square_rank(sq) if pc.color else 7 - chess.square_rank(sq)
            v += rank * 4
        if sq in CENTER:
            v += 10
        s += v if pc.color == board.turn else -v
    return s


def _ordered(board: chess.Board, captures_only: bool = False) -> list[chess.Move]:
    moves = list(board.generate_legal_captures()) if captures_only else list(board.legal_moves)

    def key(m: chess.Move) -> int:
        k = 0
        if board.is_capture(m):
            victim = board.piece_type_at(m.to_square) or chess.PAWN
            k += 10 * VALUES[victim] - VALUES[board.piece_type_at(m.from_square) or chess.PAWN] // 10
        if m.promotion:
            k += 800
        return -k

    return sorted(moves, key=key)


def _qsearch(board: chess.Board, alpha: int, beta: int, depth: int) -> int:
    stand = static_eval(board)
    if depth == 0 or stand >= beta:
        return stand
    alpha = max(alpha, stand)
    for m in _ordered(board, captures_only=True):
        board.push(m)
        sc = -_qsearch(board, -beta, -alpha, depth - 1)
        board.pop()
        if sc >= beta:
            return sc
        alpha = max(alpha, sc)
    return alpha


def _search(board: chess.Board, depth: int, alpha: int, beta: int, ply: int) -> tuple[int, list[chess.Move]]:
    if board.is_checkmate():
        return -MATE_CP + ply, []
    if board.is_game_over():
        return 0, []
    if depth == 0:
        return _qsearch(board, alpha, beta, 4), []
    best: list[chess.Move] = []
    for m in _ordered(board):
        board.push(m)
        sc, pv = _search(board, depth - 1, -beta, -alpha, ply + 1)
        sc = -sc
        board.pop()
        if sc > alpha:
            alpha, best = sc, [m, *pv]
            if alpha >= beta:
                break
    return alpha, best


class MaterialEngine:
    name = "material-fallback"
    version = "material-fallback 1.0"

    def __init__(self, depth: int = 2) -> None:
        self.depth = depth

    def _analyse_sync(self, board: chess.Board, multipv: int) -> Analysis:
        b = board.copy(stack=False)
        lines: list[EngineLine] = []
        for m in _ordered(b):
            b.push(m)
            sc, pv = _search(b, self.depth - 1, -MATE_CP - 1, MATE_CP + 1, 1)
            b.pop()
            lines.append(EngineLine(move=m, cp=-sc, pv=(m, *pv)))
        lines.sort(key=lambda ln: -ln.cp)
        return Analysis(board.fen(), self.depth, lines[:multipv])

    async def analyse(self, board: chess.Board, depth: int | None = None, multipv: int = 1) -> Analysis:
        if board.is_game_over():
            return Analysis(board.fen(), self.depth, [])
        return await asyncio.to_thread(self._analyse_sync, board, multipv)

    async def close(self) -> None:
        return None
