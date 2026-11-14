"""Async UCI controller: a pool of Stockfish processes behind an asyncio queue.

Works with any UCI engine; production image builds Stockfish 19 from source.
Positions are validated (FEN legality) before they reach the engine.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from collections import OrderedDict

import chess
import chess.engine

from .base import MATE_CP, Analysis, EngineLine, clamp_cp

log = logging.getLogger(__name__)


class InvalidPosition(ValueError):
    pass


def validate_board(board: chess.Board) -> None:
    status = board.status()
    if status != chess.STATUS_VALID:
        raise InvalidPosition(f"illegal position: {status!r}")


class StockfishPool:
    def __init__(self, path: str, size: int = 2, threads: int = 1, hash_mb: int = 64, default_depth: int = 14,
                 cache_size: int = 4096) -> None:
        self.path = path
        self.size = size
        self.options = {"Threads": threads, "Hash": hash_mb}
        self.default_depth = default_depth
        self._queue: asyncio.Queue[chess.engine.UciProtocol] = asyncio.Queue()
        self._transports: list[asyncio.SubprocessTransport] = []
        self._started = False
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[str, int, int], Analysis] = OrderedDict()
        self._cache_size = cache_size
        self.name = "stockfish"
        self.version = "unknown"

    @staticmethod
    def available(path: str) -> bool:
        return shutil.which(path) is not None

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            for _ in range(self.size):
                transport, proto = await chess.engine.popen_uci(self.path)
                await proto.configure({k: v for k, v in self.options.items() if k in proto.options})
                self.version = proto.id.get("name", "unknown")
                self._transports.append(transport)
                self._queue.put_nowait(proto)
            self._started = True
            log.info("engine pool ready: %s x%d", self.version, self.size)

    async def analyse(self, board: chess.Board, depth: int | None = None, multipv: int = 1) -> Analysis:
        validate_board(board)
        depth = depth or self.default_depth
        multipv = max(1, min(multipv, board.legal_moves.count() or 1))
        key = (board.fen(), depth, multipv)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if board.is_game_over():
            return Analysis(board.fen(), depth, [])
        await self.start()
        proto = await self._queue.get()
        try:
            infos = await proto.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
        finally:
            self._queue.put_nowait(proto)
        lines: list[EngineLine] = []
        for info in infos if isinstance(infos, list) else [infos]:
            pv = info.get("pv") or []
            if not pv:
                continue
            score = info["score"].pov(board.turn)
            mate = score.mate()
            cp = score.score(mate_score=MATE_CP)
            lines.append(EngineLine(move=pv[0], cp=clamp_cp(cp), pv=tuple(pv), mate=mate))
        res = Analysis(board.fen(), depth, lines)
        self._cache[key] = res
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return res

    async def close(self) -> None:
        while not self._queue.empty():
            proto = self._queue.get_nowait()
            try:
                await asyncio.wait_for(proto.quit(), 2)
            except Exception:  # pragma: no cover
                pass
        self._started = False
