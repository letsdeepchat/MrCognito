"""Socratic LLM orchestrator.

Responsibilities split deliberately:
* *Decisions* (does the plan hold? which hint tier?) are made deterministically
  server-side from engine ground truth.
* The LLM only *phrases* the Socratic response, may call the strict JSON tools
  to look at the board, and must finish with a ``socratic_reply`` tool call.
* Every reply is policed by ``guardrails.check_output``; a violating reply gets
  one rewrite request, then falls back to the deterministic template coach.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..config import get_settings
from .guardrails import OutputPolicy, check_output
from .llm import LLMClient, LLMTurn, RuleBasedSocraticLLM, build_llm, compose
from .tools import ALL_TOOLS, ToolContext, ToolValidationError, execute_tool, validate_args

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Socratic chess coach inside a cognitive-training app whose purpose is to make the
user think harder, not to think for them.

Hard rules (a server-side filter rejects violations):
- Never state a concrete move other than ones the user wrote themselves or ones listed in `allowed_moves`.
- Never give an evaluation, a score, a percentage, "mate in N", or say who is better or winning.
- Never mention engines.
- At hint tier 1, do not name squares or piece types; speak only about imbalances and concepts.
- At hint tier 2, you may name a sector, file or diagonal, but no moves.
- Always end with exactly one question that makes the user calculate or articulate.
- Keep the message under 70 words. Plain, warm, direct.

You receive a JSON task with structured tactical flags, retrieved principles and the server's decision.
Use the tools if you need more board facts (they never reveal moves). Finish by calling `socratic_reply`."""


@dataclass
class CoachReply:
    message: str
    question: str
    concepts: list[str] = field(default_factory=list)
    source: str = "rule"
    tool_calls: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


class SocraticOrchestrator:
    def __init__(self, llm: LLMClient | None = None, max_rounds: int | None = None) -> None:
        s = get_settings()
        self.llm = llm or build_llm(s.llm_provider, s.llm_model, s.llm_base_url, s.llm_api_key)
        self.fallback = RuleBasedSocraticLLM()
        self.max_rounds = max_rounds or s.llm_max_tool_rounds

    async def respond(self, task: dict, policy: OutputPolicy, tool_ctx: ToolContext) -> CoachReply:
        try:
            reply = await self._loop(self.llm, task, policy, tool_ctx)
            if reply is not None:
                return reply
        except Exception as exc:  # provider outage, refusal, malformed output...
            log.warning("LLM coach failed (%s); using rule coach", exc)
        if self.llm is not self.fallback:
            reply = await self._loop(self.fallback, task, policy, tool_ctx)
            if reply is not None:
                return reply
        # Absolute last resort: a template we know is policy-clean.
        safe = compose({"kind": "chat", "rag": task.get("rag")}, {})
        return CoachReply(safe["message"], safe["question"], safe["concepts"], source="safe-template")

    async def _loop(self, llm: LLMClient, task: dict, policy: OutputPolicy, tool_ctx: ToolContext) -> CoachReply | None:
        transcript: list[dict] = [{"role": "user", "text": json.dumps({"task": task,
                                                                       "allowed_moves": sorted(policy.allowed_moves),
                                                                       "hint_tier": policy.tier})}]
        rewrites = 0
        used: list[str] = []
        for _ in range(self.max_rounds + 2):
            turn: LLMTurn = await llm.complete(SYSTEM_PROMPT, transcript, ALL_TOOLS, task)
            if turn.stop_reason == "refusal":
                return None
            if not turn.tool_calls:
                transcript.append({"role": "assistant", "turn": turn})
                transcript.append({"role": "user", "text": "Finish by calling the socratic_reply tool."})
                continue
            transcript.append({"role": "assistant", "turn": turn})
            results = []
            final: dict | None = None
            for call in turn.tool_calls:
                used.append(call.name)
                if call.name == "socratic_reply":
                    try:
                        final = validate_args("socratic_reply", call.args)
                        results.append({"id": call.id, "name": call.name, "content": '{"received": true}', "is_error": False})
                    except ToolValidationError as exc:
                        results.append({"id": call.id, "name": call.name, "content": json.dumps({"error": str(exc)}), "is_error": True})
                    continue
                try:
                    out = await execute_tool(tool_ctx, call.name, call.args)
                    results.append({"id": call.id, "name": call.name, "content": json.dumps(out), "is_error": False})
                except ToolValidationError as exc:
                    results.append({"id": call.id, "name": call.name, "content": json.dumps({"error": str(exc)}), "is_error": True})
            transcript.append({"role": "tool_results", "results": results})
            if final is not None:
                text = f"{final['message']} {final['question']}"
                violations = check_output(text, policy)
                if not violations:
                    return CoachReply(final["message"], final["question"], final.get("concepts", []),
                                      source=llm.name, tool_calls=used)
                log.info("coach reply rejected: %s", violations)
                rewrites += 1
                if rewrites > 1:
                    return None
                transcript.append({"role": "user", "text": "POLICY_VIOLATION " + json.dumps(violations) +
                                   ". Rewrite without moves, squares or evaluations that are not allowed."})
        return None
