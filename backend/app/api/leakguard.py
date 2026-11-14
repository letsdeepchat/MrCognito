"""Defence-in-depth: every JSON response under /api/sessions is scanned for
engine-output keys and evaluation-shaped values before it leaves the server.

Phase handlers are written never to include such data; if one ever does, the
request fails closed (HTTP 500, logged) instead of leaking.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

log = logging.getLogger("leakguard")

FORBIDDEN_KEYS = {
    "best_move", "bestmove", "best_uci", "pv", "principal_variation", "score_cp", "cp", "centipawns", "eval",
    "evaluation", "eval_cp", "win_prob", "win_probability", "winning_chances", "mate_in", "engine_line",
    "acceptable", "acceptable_ucis", "solution_uci", "solution", "tempting_uci", "refutation", "delta_cp",
    "start_eval", "trap_uci", "stockfish_eval_delta_cp", "line",
}
EVAL_VALUE = re.compile(r"(?<![\w.])[+\-−]\d+\.\d+(?![\w])|\b\d+\s*cp\b|\bmate in \d+\b", re.I)


def find_leaks(obj: Any, path: str = "$") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                out.append(f"{path}.{k}")
            out += find_leaks(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_leaks(v, f"{path}[{i}]")
    elif isinstance(obj, str) and EVAL_VALUE.search(obj):
        out.append(f"{path}=~eval")
    return out


class LeakGuardRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    payload = json.loads(response.body)
                except Exception:
                    return response
                leaks = find_leaks(payload)
                if leaks:
                    log.error("LEAK BLOCKED on %s: %s", request.url.path, leaks[:5])
                    return Response(json.dumps({"error": "internal_policy_violation"}), status_code=500,
                                    media_type="application/json")
            return response

        return handler
