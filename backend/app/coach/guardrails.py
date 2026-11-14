"""Anti-offloading guardrails: input intent detection and output leak policing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

OFFLOAD_PATTERNS = [
    r"\bbest\s+move\b", r"\bwhat\s+(should|do|would|can)\s+i\s+(play|do|move)\b", r"\bwhat'?s\s+the\s+(move|answer|solution)\b",
    r"\b(give|tell|show)\s+me\s+(the\s+)?(move|answer|solution|line|engine|eval)", r"\bam\s+i\s+(winning|losing|better|worse)\b",
    r"\bwho'?s\s+(winning|better)\b", r"\b(eval|evaluation|centipawn|engine\s+line|stockfish|win\s*(prob|probability|chance))\b",
    r"\bsolution\b", r"\bjust\s+tell\b", r"\bwhich\s+move\s+(is|wins)\b", r"\bis\s+\S+\s+the\s+(best|right|correct)\s+move\b",
]
_OFFLOAD_RE = re.compile("|".join(OFFLOAD_PATTERNS), re.I)

SQUARE = r"[a-h][1-8]"
PIECE_SAN = re.compile(r"(?<![A-Za-z0-9])[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?(?![A-Za-z0-9])")
PAWN_CAPTURE = re.compile(r"(?<![A-Za-z0-9])[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?(?![A-Za-z0-9])")
CASTLE = re.compile(r"(?<![A-Za-z0-9-])(?:O-O-O|O-O|0-0-0|0-0)(?![A-Za-z0-9-])")
UCI = re.compile(r"(?<![A-Za-z0-9])[a-h][1-8][a-h][1-8][qrbn]?(?![A-Za-z0-9])")
BARE_SQUARE = re.compile(rf"(?<![A-Za-z0-9-]){SQUARE}(?![A-Za-z0-9])")
EVAL_PATTERNS = [
    re.compile(r"(?<![\w.])[+\-−]\d+(?:\.\d+)?(?![\w])"),  # +1.3 / -0.8
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:cp|centipawns?)\b", re.I),
    re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%"),
    re.compile(r"\bmate\s+in\s+\d+\b", re.I),
    re.compile(r"\b(?:best|winning|only|engine|top)\s+move\b", re.I),
    re.compile(r"\b(?:stockfish|engine|evaluation|eval)\b", re.I),
    re.compile(r"\byou\s+are\s+(?:winning|losing|better|worse)\b", re.I),
]
PIECE_WORDS = re.compile(r"\b(king|queen|rook|bishop|knight|pawn)s?\b", re.I)


def detect_offloading(text: str) -> bool:
    return bool(_OFFLOAD_RE.search(text or ""))


def move_tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for rx in (PIECE_SAN, PAWN_CAPTURE, CASTLE, UCI):
        toks.update(m.group(0).rstrip("+#") for m in rx.finditer(text or ""))
    return toks


def normalise_move_token(tok: str) -> str:
    return tok.replace("0-0-0", "O-O-O").replace("0-0", "O-O").rstrip("+#")


def board_move_strings(board: chess.Board, move: chess.Move) -> set[str]:
    """All the ways a move might be written (SAN w/o suffix, UCI)."""
    try:
        san = board.san(move).rstrip("+#")
    except Exception:
        san = ""
    out = {move.uci()}
    if san:
        out.add(san)
    return out


@dataclass
class OutputPolicy:
    tier: int = 0  # 0 = no hint context (plan feedback / chat)
    allowed_moves: set[str] = field(default_factory=set)  # user's own moves, tier-3 pair, opponent's last move
    forbidden_moves: set[str] = field(default_factory=set)  # engine best for the user's position (all spellings)
    allow_squares: bool = True
    allow_piece_names: bool = True


def tier_policy(tier: int, allowed: set[str], forbidden: set[str]) -> OutputPolicy:
    if tier == 1:
        return OutputPolicy(1, allowed, forbidden, allow_squares=False, allow_piece_names=False)
    return OutputPolicy(tier, allowed, forbidden)


def check_output(text: str, policy: OutputPolicy) -> list[str]:
    """Return a list of violations (empty means the text is safe to show)."""
    v: list[str] = []
    for rx in EVAL_PATTERNS:
        if rx.search(text):
            v.append(f"evaluation_language:{rx.pattern[:24]}")
    allowed = {normalise_move_token(a) for a in policy.allowed_moves}
    for tok in move_tokens(text):
        t = normalise_move_token(tok)
        if t not in allowed:
            v.append(f"move_token:{t}")
    for tok in BARE_SQUARE.findall(text):
        if not policy.allow_squares:
            v.append(f"square_at_tier1:{tok}")
        elif tok in policy.forbidden_moves and tok not in allowed:  # a pawn push spelled as a square
            v.append(f"move_token:{tok}")
    if not policy.allow_piece_names and PIECE_WORDS.search(text):
        v.append("piece_name_at_tier1")
    return v


def refusal_message(diagnostic_question: str) -> str:
    return ("I won't hand you a move or an evaluation; finding it is the workout. "
            f"Let's diagnose instead: {diagnostic_question}")
