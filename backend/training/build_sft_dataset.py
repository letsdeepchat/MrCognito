"""Build a supervised fine-tuning set for the Socratic coach.

Each example pairs the *structured* context the orchestrator gives the LLM
(FEN, tactical flags, Maia-style human-blunder probability, retrieved
principles, server decision, hint tier) with a target ``socratic_reply``.

Targets come from two sources:
1. ``--dialogues``: a JSONL of curated grandmaster/coach Socratic dialogues
   with fields {fen, context?, coach_message, coach_question}. This is the
   data that matters; it is not shipped (licensing) and must be curated.
2. Bootstrap: the deterministic rule coach, so the format and the guardrail
   behaviour are learned before human data arrives.

Every target is checked against the same output guardrail used in
production; examples that would leak a move or an evaluation are dropped.

    python -m training.build_sft_dataset --positions app/content/puzzles.json --out data/sft.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path

import chess

from app.coach.guardrails import check_output, tier_policy
from app.coach.llm import compose
from app.coach.orchestrator import SYSTEM_PROMPT
from app.coach.rag import MemoryStore
from app.engines.human_model import HeuristicHumanModel
from app.engines.registry import get_engine
from app.engines.tactics import assess_move, most_tense_sector, position_concepts, sector_tension
from app.protocol.common import imbalance_prompt


async def context_for(board: chess.Board, engine, human, store, tier: int, rng: random.Random) -> tuple[dict, set[str]]:
    an = await engine.analyse(board, depth=12, multipv=3)
    pred = await human.predict(board, 1400)
    tempting, prob = pred.top(1)[0]
    a = await assess_move(engine, board, tempting, depth=12, before=an)
    concepts = position_concepts(board)
    rag = await store.search(" ".join(concepts), k=2, tags=concepts)
    sector = most_tense_sector(board)
    task = {
        "kind": "hint", "tier": tier, "focus_sector": sector,
        "flags": a.flags, "tempting_move_human_probability": round(prob, 2), "tempting_move_severity": a.severity,
        "imbalance_prompt": imbalance_prompt(concepts), "sector_phrase": sector,
        "rag": [{"id": c.id, "title": c.title, "principle": c.text, "question": c.question} for c in rag],
    }
    forbidden = {board.san(an.best.move).rstrip("+#"), an.best.move.uci()} if an.best else set()
    return task, forbidden


def to_chat(task: dict, reply: dict) -> dict:
    return {"messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"task": task, "hint_tier": task.get("tier", 0)})},
        {"role": "assistant", "content": json.dumps(reply)},
    ]}


async def main(a: argparse.Namespace) -> None:
    rng = random.Random(a.seed)
    engine = get_engine()
    human = HeuristicHumanModel(engine)
    store = MemoryStore()
    out, dropped = [], 0
    positions = json.loads(Path(a.positions).read_text())
    for p in positions:
        board = chess.Board(p["fen"])
        for tier in (1, 2):
            task, forbidden = await context_for(board, engine, human, store, tier, rng)
            results = {"get_board_sector_tension": sector_tension(board, task["focus_sector"])}
            reply = compose(task, results)
            if check_output(reply["message"] + " " + reply["question"], tier_policy(tier, set(), forbidden)):
                dropped += 1
                continue
            out.append(to_chat(task, reply))
    if a.dialogues:
        for line in Path(a.dialogues).read_text().splitlines():
            d = json.loads(line)
            board = chess.Board(d["fen"])
            task, forbidden = await context_for(board, engine, human, store, d.get("tier", 0), rng)
            task.update(d.get("context", {}))
            reply = {"message": d["coach_message"], "question": d["coach_question"], "concepts": d.get("concepts", [])}
            if check_output(reply["message"] + " " + reply["question"], tier_policy(task["tier"], set(), forbidden)):
                dropped += 1
                continue
            out.append(to_chat(task, reply))
    rng.shuffle(out)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(json.dumps(x) for x in out))
    print(f"wrote {len(out)} examples, dropped {dropped} that failed the guardrail")
    await engine.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default="app/content/puzzles.json")
    ap.add_argument("--dialogues", default="")
    ap.add_argument("--out", default="data/sft.jsonl")
    ap.add_argument("--seed", type=int, default=0)
    asyncio.run(main(ap.parse_args()))
