"""Engine-facing data types.

These objects are INTERNAL. They contain raw engine truth (moves, centipawns)
and must never cross the API boundary. ``app.api.leakguard`` enforces that on
every response; nothing in ``app.schemas`` can hold them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import chess

MATE_CP = 10_000


@dataclass(frozen=True)
class EngineLine:
    move: chess.Move
    cp: int  # from the side-to-move's point of view; mates mapped to +/-(MATE_CP - ply)
    pv: tuple[chess.Move, ...] = ()
    mate: int | None = None


@dataclass
class Analysis:
    fen: str
    depth: int
    lines: list[EngineLine] = field(default_factory=list)

    @property
    def best(self) -> EngineLine | None:
        return self.lines[0] if self.lines else None

    def cp_of(self, move: chess.Move) -> int | None:
        for ln in self.lines:
            if ln.move == move:
                return ln.cp
        return None


class ChessEngine(Protocol):
    name: str

    async def analyse(self, board: chess.Board, depth: int | None = None, multipv: int = 1) -> Analysis: ...

    async def close(self) -> None: ...


def clamp_cp(cp: int) -> int:
    return max(-MATE_CP, min(MATE_CP, cp))
