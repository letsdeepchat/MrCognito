"""Mine Phase-2 "cognitive trap" puzzles and Phase-3 sparring positions.

A trap is a position where a move the human model rates as *likely* for a
club player loses >= TRAP_DROP centipawns versus the best move. The motif of
the punishing reply is classified from the engine refutation line.

    python -m scripts.mine_content --games 120 --out app/content
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path

import chess

from app.engines.human_model import HeuristicHumanModel
from app.engines.registry import get_engine
from app.engines.tactics import classify_refutation, position_concepts
from app.protocol.content import OPENING_LINES

TRAP_DROP = 200
MIN_TEMPT_PROB = 0.18


async def mine(games: int, seed: int, depth: int) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    engine = get_engine()
    human = HeuristicHumanModel(engine)
    puzzles: dict[str, dict] = {}
    sparring: dict[str, dict] = {}
    for g in range(games):
        board = chess.Board()
        line = rng.choice(list(OPENING_LINES.values()))
        for san in line[: rng.randint(4, 10)]:
            board.push_san(san)
        elo = rng.choice([900, 1100, 1300, 1500, 1700])
        for ply in range(rng.randint(14, 40)):
            if board.is_game_over():
                break
            an = await engine.analyse(board, depth=depth, multipv=6)
            pred = await human.predict(board, elo)
            best = an.best
            if best and -150 <= best.cp <= 400 and board.fullmove_number >= 6:
                # candidates the human model likes
                for mv, p in pred.top(3):
                    if p < MIN_TEMPT_PROB or mv == best.move:
                        continue
                    b2 = board.copy(stack=False)
                    b2.push(mv)
                    after = await engine.analyse(b2, depth=depth)
                    if not after.best:
                        continue
                    drop = best.cp + after.best.cp
                    if drop >= TRAP_DROP:
                        motif = classify_refutation(b2, after.best.pv)
                        acceptable = [ln.move.uci() for ln in an.lines if best.cp - ln.cp <= 60]
                        key = board.fen()
                        puzzles[key] = {
                            "id": f"trap-{len(puzzles):04d}", "fen": key, "tempting_uci": mv.uci(),
                            "tempting_human_prob": round(p, 3), "solution_uci": best.move.uci(),
                            "acceptable_ucis": acceptable, "motif": motif, "drop_cp": drop,
                            "rating": int(700 + 1400 * (1 - p) + min(drop, 900) / 3), "concepts": position_concepts(board),
                        }
                        break
            nonpawn = sum(1 for pc in board.piece_map().values() if pc.piece_type not in (chess.PAWN, chess.KING))
            if best and abs(best.cp) <= 120 and 16 <= board.ply() <= 50 and nonpawn >= 6 and rng.random() < 0.15:
                sparring[board.fen()] = {"id": f"spar-{len(sparring):04d}", "fen": board.fen(),
                                         "theme": ", ".join(position_concepts(board)), "rating": elo}
            # advance the game with a human-like move
            moves, weights = zip(*pred.probs.items())
            board.push(rng.choices(moves, weights=weights)[0])
        print(f"game {g + 1}/{games}: {len(puzzles)} traps, {len(sparring)} sparring", flush=True)
    await engine.close()
    return list(puzzles.values()), list(sparring.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--out", default="app/content")
    a = ap.parse_args()
    pz, sp = asyncio.run(mine(a.games, a.seed, a.depth))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "puzzles.json").write_text(json.dumps(pz, indent=1))
    (out / "sparring.json").write_text(json.dumps(sp, indent=1))
    print(f"wrote {len(pz)} puzzles, {len(sp)} sparring positions")


if __name__ == "__main__":
    main()
