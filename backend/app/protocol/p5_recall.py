"""Phase 5 - Active recall & episodic consolidation."""
from __future__ import annotations

import chess

from .common import ApiError, Ctx, masked_board

HABIT_TAGS = [
    "check-captures-threats first", "ask what my opponent wants", "blunder-check before moving",
    "calculate to a quiet position", "improve my worst piece", "slow down on critical moves",
    "visualise before touching", "name the plan out loud",
]


def placement_accuracy(target_fen: str, user_placement: str) -> float:
    """F1 over pieces-on-squares between target and reconstruction."""
    t = chess.Board(target_fen).piece_map()
    try:
        u = chess.BaseBoard(user_placement.split(" ")[0]).piece_map()
    except ValueError:
        raise ApiError(422, "bad_placement", "Send the reconstruction as the piece-placement field of a FEN.")
    if not t and not u:
        return 1.0
    tp = sum(1 for sq, pc in u.items() if t.get(sq) == pc)
    return round(2 * tp / (len(t) + len(u)), 4) if (t or u) else 0.0


async def init(ctx: Ctx) -> dict:
    tp = ctx.st.get("turning_point") or {}
    fen = tp.get("fen")
    if not fen:
        fen = (ctx.st.get("sparring_last") or {}).get("fen") or chess.STARTING_FEN
    return {"target_fen": fen, "step": "reconstruct", "accuracy": None, "done": False}


def view(ctx: Ctx, st: dict) -> dict:
    if st["done"]:
        return {"done": True}
    v = {"step": st["step"], "habit_tags": HABIT_TAGS}
    if st["step"] == "reconstruct":
        v["board"] = masked_board(st["target_fen"], 3)
        v["instruction"] = ("The board is cleared. Rebuild the critical turning-point position from your sparring game: "
                            "pawn skeleton first, then kings, then pieces.")
    else:
        v["board"] = masked_board(st["target_fen"], 0)
        v["accuracy"] = st["accuracy"]
        v["instruction"] = "In your own words: what was the key mistake or breakthrough? Then tag one habit for tomorrow."
    return v


async def act(ctx: Ctx, st: dict, action: dict) -> dict:
    t = action["type"]
    if t == "recall_reconstruct" and st["step"] == "reconstruct":
        acc = placement_accuracy(st["target_fen"], action["placement"])
        st["accuracy"] = acc
        st["step"] = "reflect"
        ctx.score("wm", acc)
        ctx.outcome(acc)
        await ctx.log_turn(item_kind="recall_reconstruct", fen=st["target_fen"], correct=acc >= 0.7,
                           extra={"accuracy": acc, "placement": action["placement"][:90]})
        return {"accuracy": acc, "actual": masked_board(st["target_fen"], 0)}
    if t == "recall_reflect" and st["step"] == "reflect":
        if len(action["key_moment"].split()) < 5:
            raise ApiError(422, "too_short", "Describe the key moment in at least one full sentence.")
        habit = action["habit_tag"].strip()[:200]
        ctx.profile.habit_for_tomorrow = habit
        await ctx.log_turn(item_kind="recall_reflection", fen=st["target_fen"], user_verbalized_plan=action["key_moment"][:2000],
                           extra={"habit": habit})
        st["step"] = "done"
        st["done"] = True
        return {"saved": True, "habit_for_tomorrow": habit}
    raise ApiError(409, "bad_action", f"{t} is not valid in step {st['step']}.")


def current_board(st: dict) -> chess.Board | None:
    return None


def solving(st: dict) -> dict | None:
    return None
