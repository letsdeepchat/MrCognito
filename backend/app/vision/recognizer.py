"""Physical-board photo -> validated FEN.

``OnnxBoardRecognizer`` expects a YOLO-style detector exported to ONNX
(see ``training/vision/README.md``) with 12 piece classes plus 4 board-corner
keypoints. Detections are mapped onto an 8x8 grid through a perspective
transform from the corners. The FEN is then validated with python-chess;
invalid boards are returned with ``valid=False`` and a list of issues so the
UI can ask the user to correct squares manually.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

import chess
import numpy as np

from ..config import get_settings

CLASSES = ["P", "N", "B", "R", "Q", "K", "p", "n", "b", "r", "q", "k"]


@dataclass
class RecognitionResult:
    fen: str
    valid: bool
    confidence: float
    issues: list[str] = field(default_factory=list)


class BoardRecognizer(Protocol):
    async def recognize(self, image_bytes: bytes, side_to_move: str = "w") -> RecognitionResult: ...


def homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    a = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        a.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.asarray(a))
    h = vt[-1].reshape(3, 3)
    return h / h[2, 2]


def detections_to_fen(dets: list[tuple[float, float, int, float]], corners: np.ndarray, side_to_move: str = "w",
                      white_at_bottom: bool = True) -> RecognitionResult:
    """dets: (x_center, y_bottom, class_id, score) in image pixels; corners: TL, TR, BR, BL of the playing area."""
    h = homography(corners, np.array([[0, 0], [8, 0], [8, 8], [0, 8]], dtype=float))
    board = chess.BaseBoard.empty()
    best: dict[int, float] = {}
    for x, y, cls, score in dets:
        p = h @ np.array([x, y, 1.0])
        col, row = int(p[0] / p[2]), int(p[1] / p[2])
        if not (0 <= col < 8 and 0 <= row < 8):
            continue
        rank = 7 - row if white_at_bottom else row
        file = col if white_at_bottom else 7 - col
        sq = chess.square(file, rank)
        if score > best.get(sq, 0):
            best[sq] = score
            board.set_piece_at(sq, chess.Piece.from_symbol(CLASSES[cls]))
    b = chess.Board(None)
    b.set_board_fen(board.board_fen())
    b.turn = side_to_move == "w"
    b.castling_rights = b.clean_castling_rights()
    issues = []
    st = b.status()
    for flag, msg in [(chess.STATUS_NO_WHITE_KING, "no white king"), (chess.STATUS_NO_BLACK_KING, "no black king"),
                      (chess.STATUS_TOO_MANY_KINGS, "too many kings"), (chess.STATUS_PAWNS_ON_BACKRANK, "pawn on back rank"),
                      (chess.STATUS_OPPOSITE_CHECK, "side not to move is in check")]:
        if st & flag:
            issues.append(msg)
    conf = float(np.mean(list(best.values()))) if best else 0.0
    return RecognitionResult(b.fen(), not issues and b.is_valid(), round(conf, 3), issues)


class OnnxBoardRecognizer:  # pragma: no cover - needs model weights
    def __init__(self, path: str, input_size: int = 640) -> None:
        import onnxruntime as ort  # type: ignore

        self.sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.size = input_size

    def _run(self, image_bytes: bytes, side: str) -> RecognitionResult:
        from io import BytesIO

        from PIL import Image  # type: ignore

        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        w0, h0 = img.size
        x = np.asarray(img.resize((self.size, self.size)), dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self.sess.run(None, {self.sess.get_inputs()[0].name: x})
        # Contract (see training/vision/README.md): out[0] = [N, 6] (cx, cy, w, h, score, cls), out[1] = [4, 2] corners
        boxes, corners = out[0], out[1]
        sx, sy = w0 / self.size, h0 / self.size
        dets = [(b[0] * sx, (b[1] + b[3] / 2) * sy, int(b[5]), float(b[4])) for b in boxes if b[4] >= 0.35]
        return detections_to_fen(dets, np.asarray(corners, dtype=float) * [sx, sy], side)

    async def recognize(self, image_bytes: bytes, side_to_move: str = "w") -> RecognitionResult:
        return await asyncio.to_thread(self._run, image_bytes, side_to_move)


_rec: BoardRecognizer | None = None
_checked = False


def get_recognizer() -> BoardRecognizer | None:
    global _rec, _checked
    if not _checked:
        _checked = True
        path = get_settings().vision_onnx_path
        if path:
            try:
                _rec = OnnxBoardRecognizer(path)
            except Exception:  # pragma: no cover
                _rec = None
    return _rec
