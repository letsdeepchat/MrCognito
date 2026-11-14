"""Process-wide engine + human-model singletons (overridable in tests)."""
from __future__ import annotations

import logging
import shutil

from ..config import get_settings
from .base import ChessEngine
from .human_model import HumanMoveModel, build_human_model
from .material import MaterialEngine
from .stockfish import StockfishPool

log = logging.getLogger(__name__)

_engine: ChessEngine | None = None
_human: HumanMoveModel | None = None


def _resolve_stockfish(path: str) -> str | None:
    for cand in (path, "stockfish", "/usr/games/stockfish", "/usr/local/bin/stockfish"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    return None


def get_engine() -> ChessEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        path = _resolve_stockfish(s.stockfish_path)
        if path:
            _engine = StockfishPool(path, size=s.stockfish_pool_size, threads=s.stockfish_threads,
                                    hash_mb=s.stockfish_hash_mb, default_depth=s.analysis_depth)
        else:
            log.warning("No UCI engine found; using pure-python MaterialEngine (dev/test only)")
            _engine = MaterialEngine()
    return _engine


def get_human_model() -> HumanMoveModel:
    global _human
    if _human is None:
        s = get_settings()
        _human = build_human_model(s.human_model, get_engine(), model_type=s.maia2_model_type,
                                   save_root=s.maia2_weights_dir)
    return _human


def set_engines(engine: ChessEngine | None, human: HumanMoveModel | None = None) -> None:
    global _engine, _human
    _engine, _human = engine, human


async def shutdown() -> None:
    if _engine is not None:
        await _engine.close()
