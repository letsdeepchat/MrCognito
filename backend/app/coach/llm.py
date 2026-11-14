"""Provider-agnostic LLM clients with a neutral tool-calling transcript.

* ``RuleBasedSocraticLLM`` - deterministic, offline, default. Exercises the same
  tool-call loop as the real providers, so the guardrails are always on.
* ``AnthropicLLM``        - Claude through the official Anthropic SDK.
* ``OpenAICompatLLM``     - any OpenAI-compatible endpoint, e.g. vLLM serving
  the LoRA-fine-tuned coach from ``training/finetune_lora.py``.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    args: Any  # dict or raw JSON string (validated by the orchestrator)


@dataclass
class LLMTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    stop_reason: str = "end_turn"


# Neutral transcript entries:
#   {"role": "user", "text": str}
#   {"role": "assistant", "turn": LLMTurn}
#   {"role": "tool_results", "results": [{"id", "name", "content": str, "is_error": bool}]}
Transcript = list[dict]


class LLMClient(Protocol):
    name: str

    async def complete(self, system: str, transcript: Transcript, tools: list[dict], task: dict) -> LLMTurn: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", api_key: str | None = None) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.model = model

    def _messages(self, transcript: Transcript) -> list[dict]:
        msgs: list[dict] = []
        for e in transcript:
            if e["role"] == "user":
                msgs.append({"role": "user", "content": e["text"]})
            elif e["role"] == "assistant":
                msgs.append({"role": "assistant", "content": e["turn"].raw})
            else:
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": r["is_error"]}
                    for r in e["results"]]})
        merged: list[dict] = []
        for m in msgs:  # the API wants alternating roles: fold adjacent user turns together
            if merged and merged[-1]["role"] == m["role"] == "user":
                prev = merged[-1]["content"]
                prev = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                cur = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
                merged[-1] = {"role": "user", "content": prev + cur}
            else:
                merged.append(m)
        return merged

    async def complete(self, system: str, transcript: Transcript, tools: list[dict], task: dict) -> LLMTurn:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=self._messages(transcript),
            tools=[{**t, "strict": True} for t in tools],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            return LLMTurn(stop_reason="refusal")
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        raw = [b.model_dump(exclude_none=True) for b in resp.content]
        return LLMTurn(text=text, tool_calls=calls, raw=raw, stop_reason=resp.stop_reason or "end_turn")


class OpenAICompatLLM:
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _messages(self, system: str, transcript: Transcript) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system}]
        for e in transcript:
            if e["role"] == "user":
                msgs.append({"role": "user", "content": e["text"]})
            elif e["role"] == "assistant":
                t: LLMTurn = e["turn"]
                m: dict = {"role": "assistant", "content": t.text or None}
                if t.tool_calls:
                    m["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                        "name": c.name, "arguments": c.args if isinstance(c.args, str) else json.dumps(c.args)}}
                        for c in t.tool_calls]
                msgs.append(m)
            else:
                for r in e["results"]:
                    msgs.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return msgs

    async def complete(self, system: str, transcript: Transcript, tools: list[dict], task: dict) -> LLMTurn:
        body = {
            "model": self.model,
            "messages": self._messages(system, transcript),
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"], "strict": True}} for t in tools],
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        calls = [ToolCall(tc["id"], tc["function"]["name"], tc["function"]["arguments"]) for tc in msg.get("tool_calls") or []]
        return LLMTurn(text=msg.get("content") or "", tool_calls=calls, raw=msg)


class RuleBasedSocraticLLM:
    """Template coach. Round 1 gathers facts through tools; round 2 answers."""

    name = "rule"

    async def complete(self, system: str, transcript: Transcript, tools: list[dict], task: dict) -> LLMTurn:
        results: dict[str, dict] = {}
        for e in transcript:
            if e["role"] == "tool_results":
                for r in e["results"]:
                    try:
                        results[r["name"]] = json.loads(r["content"])
                    except json.JSONDecodeError:
                        pass
        violations = [e for e in transcript if e["role"] == "user" and e["text"].startswith("POLICY_VIOLATION")]
        if not results and not violations and task.get("focus_sector") and task.get("tier", 0) != 1:
            return LLMTurn(tool_calls=[ToolCall(_id(), "get_board_sector_tension", {"sector": task["focus_sector"]})])
        return LLMTurn(tool_calls=[ToolCall(_id(), "socratic_reply", compose(task, results, strict=bool(violations)))])


def _id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _tension_phrase(t: dict | None) -> str:
    if not t or "contested_squares" not in t:
        return ""
    bits = []
    if t["own_pieces_insufficiently_defended"]:
        bits.append("one of your own pieces there is defended fewer times than it is attacked")
    if t["opponent_pieces_insufficiently_defended"]:
        bits.append("an enemy piece there is loose")
    if t["own_king_in_sector"] and t["pressure_balance"] == "unfavourable":
        bits.append("your king lives there and the pressure is not in your favour")
    if not bits:
        bits.append(f"{t['contested_squares']} squares there are contested by both sides")
    return "; ".join(bits)


def compose(task: dict, results: dict, strict: bool = False) -> dict:
    """Deterministic Socratic phrasing. Never includes engine moves (it never sees any)."""
    kind = task.get("kind")
    rag_q = (task.get("rag") or [{}])[0].get("question", "What is your opponent threatening?")
    concepts = [c.get("id", "") for c in (task.get("rag") or [])][:3]
    tension = _tension_phrase(results.get("get_board_sector_tension"))
    sector = task.get("focus_sector", "")
    if kind == "hint":
        tier = task["tier"]
        if tier == 1:
            imb = task.get("imbalance_prompt") or "Compare how active each side's least useful unit is."
            return {"message": f"Step back from calculation for a moment. {imb}", "question": _strip_pieces(rag_q) if strict else rag_q_tier1(rag_q),
                    "concepts": concepts}
        if tier == 2:
            msg = f"Narrow your attention to the {task.get('sector_phrase', sector)}."
            if tension:
                msg += f" Notice that {tension}."
            return {"message": msg, "question": "What changes there if you act first with a forcing move?", "concepts": concepts}
        if tier == 3:
            a, b = task["candidate_pair"]
            return {"message": f"Compare two candidates: {a} and {b}. One holds up and one has a concrete flaw.",
                    "question": "Calculate your opponent's most forcing reply to each. Which one collapses, and why?",
                    "concepts": concepts}
        return {"message": "Let's walk the forcing sequence together, one move at a time. You enter every move.",
                "question": "What is the first forcing move you would look at in this position?", "concepts": concepts}
    if kind == "plan_feedback":
        v = task.get("verdict", {})
        cand = v.get("flawed_candidate")
        if v.get("decision") == "recalculate" and v.get("reason") == "overlooked_forcing_reply" and cand:
            q = (f"Your idea {cand} has a purpose, but what is your opponent's most forcing answer to it? "
                 f"List their checks and captures after {cand} before you decide.")
            return {"message": "Pause before committing.", "question": q, "concepts": concepts}
        if v.get("decision") == "recalculate" and v.get("reason") == "weak_reply_prediction":
            reply = v.get("user_reply", "that reply")
            q = f"You expect {reply}. Is that really their most testing answer, or is there something more forcing?"
            return {"message": "Your opponent may have a stronger resource than the one you named.", "question": q,
                    "concepts": concepts}
        if v.get("decision") == "recalculate" and v.get("reason") == "incomplete":
            return {"message": "Your plan needs all three parts before the board unlocks.",
                    "question": "Name two candidates, the strongest reply you fear, and the purpose of your choice.",
                    "concepts": concepts}
        msg = "Your reasoning hangs together." if v.get("decision") == "coherent" else "Reasonable, with one caution."
        if tension:
            msg += f" Keep in mind that in the {sector} {tension}."
        return {"message": msg, "question": rag_q, "concepts": concepts}
    if kind == "refusal":
        return {"message": "I won't hand you the move or an evaluation; finding it is the workout.",
                "question": task.get("diagnostic") or rag_q, "concepts": concepts}
    if kind == "move_feedback":
        return {"message": "The position just changed character.",
                "question": "What did your opponent's last move create: a threat, a weakness, or a new line?",
                "concepts": concepts}
    msg = "Let's think it through together."
    if tension:
        msg += f" In the {sector}, {tension}."
    return {"message": msg, "question": rag_q, "concepts": concepts}


def rag_q_tier1(q: str) -> str:
    return _strip_pieces(q)


def _strip_pieces(q: str) -> str:
    import re

    if re.search(r"\b(king|queen|rook|bishop|knight|pawn)s?\b|\b[a-h][1-8]\b", q, re.I):
        return "Which side's units are working together better, and why?"
    return q


def build_llm(provider: str, model: str, base_url: str = "", api_key: str = "") -> LLMClient:
    if provider == "anthropic":
        return AnthropicLLM(model=model, api_key=api_key or None)
    if provider == "openai_compat":
        return OpenAICompatLLM(base_url=base_url, model=model, api_key=api_key)
    return RuleBasedSocraticLLM()
