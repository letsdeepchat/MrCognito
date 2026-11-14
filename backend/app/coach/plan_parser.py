"""Turn a speech-to-text transcript into the structured 3-part micro-plan.

Handles spoken notation ("knight takes e5", "castles short", "bishop to g5").
"""
from __future__ import annotations

import re

import chess

PIECES = {"knight": "N", "night": "N", "bishop": "B", "rook": "R", "queen": "Q", "king": "K"}
SPOKEN_SQ = re.compile(r"\b([a-h])\s*[- ]?\s*(one|two|three|four|five|six|seven|eight|[1-8])\b", re.I)
NUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8"}


def normalise_spoken(text: str) -> str:
    t = " " + text.lower() + " "
    t = re.sub(r"\bcastles?\s+(long|queen\s*side)\b", " O-O-O ", t)
    t = re.sub(r"\bcastles?(\s+(short|king\s*side))?\b", " O-O ", t)
    t = SPOKEN_SQ.sub(lambda m: m.group(1) + NUM.get(m.group(2).lower(), m.group(2)), t)
    for word, letter in PIECES.items():
        t = re.sub(rf"\b{word}s?\s+(?:to\s+)?(?=(?:x|takes|captures)?\s*[a-h][1-8])", letter, t)
        t = re.sub(rf"\b{word}\s+(takes|captures)\s+", letter + "x", t)
    t = re.sub(r"\b([a-h])\s+(takes|captures)\s+", r"\1x", t)
    t = re.sub(r"\b([NBRQK])\s*(?:takes|captures)\s*", r"\1x", t, flags=re.I)
    t = re.sub(r"\b([nbrqk])(x?[a-h][1-8])", lambda m: m.group(1).upper() + m.group(2), t)
    return t


TOKEN = re.compile(r"(O-O-O|O-O|[NBRQK]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?)")


def extract_moves(board: chess.Board, text: str) -> list[str]:
    out: list[str] = []
    for tok in TOKEN.findall(normalise_spoken(text)):
        try:
            san = board.san(board.parse_san(tok))
        except ValueError:
            continue
        if san not in out:
            out.append(san)
    return out


def parse_plan(fen: str, transcript: str) -> dict:
    board = chess.Board(fen)
    low = transcript.lower()
    reply_m = re.search(r"\b(repl(y|ies)|respond|answer|opponent|they|black|white)\b", low)
    purp_m = re.search(r"\b(purpose|because|so that|in order|idea|plan is|aim|goal)\b", low)
    cut1 = reply_m.start() if reply_m else len(transcript)
    cut2 = purp_m.start() if purp_m and purp_m.start() > cut1 else (purp_m.start() if purp_m and not reply_m else len(transcript))
    cand_text = transcript[:min(cut1, cut2)]
    reply_text = transcript[cut1:cut2] if reply_m else ""
    purpose = transcript[cut2:].strip() if purp_m else ""
    cands = extract_moves(board, cand_text)
    reply = ""
    if cands:
        after = board.copy(stack=False)
        after.push(board.parse_san(cands[0]))
        replies = extract_moves(after, reply_text)
        reply = replies[0] if replies else reply_text.strip()
    return {"candidates": cands[:3], "opponent_reply": reply, "purpose": purpose,
            "complete": len(cands) >= 2 and bool(reply) and len(purpose.split()) >= 3}
