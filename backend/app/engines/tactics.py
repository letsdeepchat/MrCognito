"""Translate raw engine truth into *structured tactical flags*.

This is the only layer that may look at engine lines. Its outputs are
categorical (flags, motifs, severities, sector tension counts) so they can be
handed to the LLM and the UI without revealing a concrete move or a score.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chess

from .base import MATE_CP, Analysis, ChessEngine
from .material import VALUES

MOTIFS = [
    "fork", "pin", "skewer", "discovered_attack", "back_rank", "hanging_piece",
    "overloading", "deflection", "interference", "zwischenzug", "prophylaxis", "trapped_piece",
    "mating_attack",
]

FLAG_DESCRIPTIONS = {
    "hanging_piece": "a piece is attacked and insufficiently defended",
    "back_rank_weakness": "the king lacks luft and the back rank can be invaded",
    "missed_prophylaxis": "the opponent had a concrete threat that was not addressed",
    "calculation_horizon_error": "the move looks fine shallowly but fails a few plies deeper",
    "allows_fork": "the move allows a double attack",
    "allows_pin": "the move allows a pin or skewer",
    "king_exposed": "the king's pawn shield is compromised",
    "material_blunder": "the move loses material by force",
}


def severity(delta_cp: int) -> str:
    """Categorical bucket for an evaluation drop (positive = worse for the mover)."""
    if delta_cp >= 300:
        return "blunder"
    if delta_cp >= 120:
        return "mistake"
    if delta_cp >= 50:
        return "inaccuracy"
    return "sound"


def hanging_pieces(board: chess.Board, color: chess.Color) -> list[chess.Square]:
    out = []
    for sq, pc in board.piece_map().items():
        if pc.color != color or pc.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        defenders = board.attackers(color, sq)
        cheapest = min(VALUES[board.piece_type_at(a)] or 100 for a in attackers)
        if not defenders or cheapest < VALUES[pc.piece_type]:
            out.append(sq)
    return out


def back_rank_weak(board: chess.Board, color: chess.Color) -> bool:
    king = board.king(color)
    if king is None:
        return False
    home = 0 if color == chess.WHITE else 7
    if chess.square_rank(king) != home:
        return False
    enemy_heavy = board.pieces(chess.ROOK, not color) | board.pieces(chess.QUEEN, not color)
    if not enemy_heavy:
        return False
    step = 1 if color == chess.WHITE else -1
    f = chess.square_file(king)
    for df in (-1, 0, 1):
        ff = f + df
        if 0 <= ff <= 7:
            sq = chess.square(ff, home + step)
            occ = board.piece_at(sq)
            if (occ is None or occ.color != color) and not board.is_attacked_by(not color, sq):
                return False  # has luft
    return True


def king_exposed(board: chess.Board, color: chess.Color) -> bool:
    king = board.king(color)
    if king is None:
        return False
    shield = 0
    step = 1 if color == chess.WHITE else -1
    r = chess.square_rank(king) + step
    if not 0 <= r <= 7:
        return False
    for df in (-1, 0, 1):
        ff = chess.square_file(king) + df
        if 0 <= ff <= 7:
            p = board.piece_at(chess.square(ff, r))
            if p and p.color == color and p.piece_type == chess.PAWN:
                shield += 1
    return shield <= 1 and len(board.pieces(chess.QUEEN, not color)) > 0


def _valuable_targets(board: chess.Board, sq: chess.Square, color: chess.Color) -> list[chess.Square]:
    """Enemy pieces attacked from ``sq`` that are worth hitting (king, bigger, or undefended)."""
    me = board.piece_type_at(sq) or chess.PAWN
    out = []
    for t in board.attacks(sq):
        p = board.piece_at(t)
        if not p or p.color == color:
            continue
        if p.piece_type == chess.KING or VALUES[p.piece_type] > VALUES[me] or not board.attackers(p.color, t):
            out.append(t)
    return out


def classify_refutation(board: chess.Board, refutation: tuple[chess.Move, ...]) -> str:
    """Name the motif of the forcing reply that punishes the previous move.

    ``board`` is the position *after* the flawed move (refuting side to move).
    """
    if not refutation:
        return "hanging_piece"
    r = refutation[0]
    side = board.turn
    b = board.copy(stack=False)
    captured = b.piece_at(r.to_square)
    undefended_capture = captured is not None and not b.attackers(not side, r.to_square)
    b.push(r)
    # Mates first
    tail = b.copy(stack=False)
    for m in refutation[1:6]:
        if m not in tail.legal_moves:
            break
        tail.push(m)
    if b.is_checkmate() or tail.is_checkmate():
        mated = b if b.is_checkmate() else tail
        k = mated.king(mated.turn)
        home = 0 if mated.turn == chess.WHITE else 7
        last = mated.peek()
        heavy = mated.piece_type_at(last.to_square) in (chess.ROOK, chess.QUEEN)
        on_home = k is not None and chess.square_rank(k) == home and chess.square_rank(last.to_square) == home
        return "back_rank" if heavy and on_home else "mating_attack"
    # Double attack from the moved piece
    if len(_valuable_targets(b, r.to_square, side)) >= 2:
        return "fork"
    # Discovered attack: a friendly slider now sees through the vacated square
    for sl in b.pieces(chess.BISHOP, side) | b.pieces(chess.ROOK, side) | b.pieces(chess.QUEEN, side):
        if sl == r.to_square:
            continue
        new = b.attacks(sl) - board.attacks(sl)
        if any(t in _valuable_targets(b, sl, side) for t in new):
            return "discovered_attack"
    # Pin / skewer along the moved slider's line
    pt = b.piece_type_at(r.to_square)
    if pt in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        for t in b.attacks(r.to_square):
            front = b.piece_at(t)
            if not front or front.color == side:
                continue
            ray = chess.SquareSet(chess.ray(r.to_square, t))
            behind = [s for s in ray if s != t and s != r.to_square and
                      chess.square_distance(r.to_square, s) > chess.square_distance(r.to_square, t) and
                      chess.SquareSet(chess.between(t, s)).isdisjoint(b.occupied)]
            for s in behind:
                bp = b.piece_at(s)
                if bp and bp.color != side:
                    fv = VALUES[front.piece_type] or 10_000
                    bv = VALUES[bp.piece_type] or 10_000
                    return "pin" if bv > fv else "skewer"
                if bp:
                    break
    if undefended_capture:
        return "hanging_piece"
    if captured is not None:
        # captured a defended piece but the line still wins: the defender was overloaded / deflected
        return "overloading"
    if b.is_check():
        return "mating_attack"
    return "zwischenzug" if any(board.is_capture(m) for m in refutation[1:2]) else "trapped_piece"


@dataclass
class MoveAssessment:
    """INTERNAL. ``delta_cp`` and ``refutation`` never leave the server."""

    delta_cp: int  # loss vs best move, >=0, mover POV
    severity: str
    flags: list[str] = field(default_factory=list)
    refutation: tuple[chess.Move, ...] = ()
    refutation_motif: str | None = None
    eval_after_cp: int = 0  # mover POV
    matched_top1: bool = False


async def null_move_threat(engine: ChessEngine, board: chess.Board, depth: int = 10) -> int:
    """Centipawns the opponent would gain if the side to move 'passed'. 0 if in check."""
    if board.is_check():
        return 0
    base = await engine.analyse(board, depth=depth)
    nb = board.copy(stack=False)
    nb.push(chess.Move.null())
    nb = chess.Board(nb.fen())
    if not nb.is_valid():
        return 0
    passed = await engine.analyse(nb, depth=depth)
    if not base.best or not passed.best:
        return 0
    return max(0, base.best.cp + passed.best.cp)  # passed is opponent POV


async def assess_move(engine: ChessEngine, board: chess.Board, move: chess.Move, depth: int = 14,
                      before: Analysis | None = None) -> MoveAssessment:
    before = before or await engine.analyse(board, depth=depth, multipv=3)
    best_cp = before.best.cp if before.best else 0
    after_board = board.copy(stack=False)
    after_board.push(move)
    after = await engine.analyse(after_board, depth=depth)
    eval_after = -(after.best.cp if after.best else (-MATE_CP if after_board.is_checkmate() else 0))
    delta = max(0, best_cp - eval_after)
    flags: list[str] = []
    mover = board.turn
    if hanging_pieces(after_board, mover) and delta >= 120:
        flags.append("hanging_piece")
    if back_rank_weak(after_board, mover):
        flags.append("back_rank_weakness")
    if king_exposed(after_board, mover):
        flags.append("king_exposed")
    if delta >= 120:
        threat = await null_move_threat(engine, board, depth=max(6, depth - 4))
        if threat >= 150:
            flags.append("missed_prophylaxis")
        shallow = await engine.analyse(after_board, depth=3)
        shallow_eval = -(shallow.best.cp if shallow.best else 0)
        if best_cp - shallow_eval < 60:
            flags.append("calculation_horizon_error")
        if delta >= 250:
            flags.append("material_blunder")
    refutation = after.best.pv if after.best else ()
    motif = classify_refutation(after_board, refutation) if delta >= 120 and refutation else None
    if motif == "fork":
        flags.append("allows_fork")
    elif motif in ("pin", "skewer"):
        flags.append("allows_pin")
    return MoveAssessment(
        delta_cp=delta, severity=severity(delta), flags=sorted(set(flags)), refutation=refutation,
        refutation_motif=motif, eval_after_cp=eval_after,
        matched_top1=bool(before.best and before.best.move == move),
    )


SECTORS = {
    "queenside": chess.SquareSet(chess.BB_FILE_A | chess.BB_FILE_B | chess.BB_FILE_C),
    "center": chess.SquareSet(chess.BB_FILE_D | chess.BB_FILE_E),
    "kingside": chess.SquareSet(chess.BB_FILE_F | chess.BB_FILE_G | chess.BB_FILE_H),
}


def sector_squares(sector: str) -> chess.SquareSet:
    s = sector.lower().strip()
    if s in SECTORS:
        return SECTORS[s]
    if s.endswith("-file") and len(s) == 6 and s[0] in "abcdefgh":
        return chess.SquareSet(chess.BB_FILES["abcdefgh".index(s[0])])
    if s.startswith("rank-") and s[-1] in "12345678":
        return chess.SquareSet(chess.BB_RANKS[int(s[-1]) - 1])
    if "-" in s and len(s) == 5:  # diagonal like a2-g8
        a, b = chess.parse_square(s[:2]), chess.parse_square(s[3:])
        return chess.SquareSet(chess.ray(a, b)) & chess.SquareSet(chess.between(a, b) | chess.BB_SQUARES[a] | chess.BB_SQUARES[b])
    raise ValueError(f"unknown sector {sector!r}")


def sector_tension(board: chess.Board, sector: str) -> dict:
    """Counts only: attacked/defended pieces, contested squares, hanging pieces."""
    sqs = sector_squares(sector)
    us, them = board.turn, not board.turn
    contested = sum(1 for s in sqs if board.is_attacked_by(us, s) and board.is_attacked_by(them, s))
    our_att = sum(len(board.attackers(us, s)) for s in sqs)
    their_att = sum(len(board.attackers(them, s)) for s in sqs)
    hang_us = [s for s in hanging_pieces(board, us) if s in sqs]
    hang_them = [s for s in hanging_pieces(board, them) if s in sqs]
    king_here = {c: board.king(c) in sqs for c in (us, them)}
    return {
        "sector": sector,
        "contested_squares": contested,
        "pressure_balance": "favourable" if our_att > their_att + 2 else "unfavourable" if their_att > our_att + 2 else "balanced",
        "own_pieces_insufficiently_defended": len(hang_us),
        "opponent_pieces_insufficiently_defended": len(hang_them),
        "own_king_in_sector": king_here[us],
        "opponent_king_in_sector": king_here[them],
    }


def most_tense_sector(board: chess.Board) -> str:
    def score(sec: str) -> int:
        t = sector_tension(board, sec)
        return t["contested_squares"] + 3 * (t["own_pieces_insufficiently_defended"] + t["opponent_pieces_insufficiently_defended"])
    return max(SECTORS, key=score)


def position_concepts(board: chess.Board) -> list[str]:
    """Structural tags used as RAG queries. No moves."""
    tags: list[str] = []
    pieces = board.piece_map()
    non_pawn = [p for p in pieces.values() if p.piece_type not in (chess.PAWN, chess.KING)]
    if len(non_pawn) <= 4:
        tags.append("endgame")
        kinds = {p.piece_type for p in non_pawn}
        if kinds <= {chess.ROOK}:
            tags.append("rook_endgame")
        if not non_pawn:
            tags.append("king_and_pawn_endgame")
    else:
        tags.append("middlegame")
    for color, name in ((chess.WHITE, "white"), (chess.BLACK, "black")):
        pawns = board.pieces(chess.PAWN, color)
        files = [chess.square_file(s) for s in pawns]
        for s in pawns:
            f = chess.square_file(s)
            if f - 1 not in files and f + 1 not in files:
                tags.append("isolated_pawn")
            enemy = board.pieces(chess.PAWN, not color)
            ahead = [e for e in enemy if abs(chess.square_file(e) - f) <= 1 and
                     ((chess.square_rank(e) > chess.square_rank(s)) if color else (chess.square_rank(e) < chess.square_rank(s)))]
            if not ahead:
                tags.append("passed_pawn")
        if len(files) != len(set(files)):
            tags.append("doubled_pawns")
    for f in range(8):
        if not any(chess.square_file(s) == f for s in board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK)):
            tags.append("open_file")
            break
    wk, bk = board.king(chess.WHITE), board.king(chess.BLACK)
    if wk is not None and bk is not None and abs(chess.square_file(wk) - chess.square_file(bk)) >= 4:
        tags.append("opposite_side_castling")
    if len(board.pieces(chess.BISHOP, chess.WHITE)) == 2 and len(board.pieces(chess.BISHOP, chess.BLACK)) < 2:
        tags.append("bishop_pair")
    for c in (chess.WHITE, chess.BLACK):
        if back_rank_weak(board, c):
            tags.append("back_rank")
        if king_exposed(board, c):
            tags.append("king_safety")
    if hanging_pieces(board, board.turn) or hanging_pieces(board, not board.turn):
        tags.append("loose_pieces")
    return sorted(set(tags))
