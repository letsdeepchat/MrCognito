"""Four-tier Socratic hint escalation ladder (pure state machine).

Rules (spec section 4.1):
* tiers only ever advance by exactly one;
* each advance requires at least one *new* user attempt since the previous hint;
* tier 4 (guided post-mortem) additionally requires >= 3 failed attempts or an explicit forfeit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import IntEnum


class Tier(IntEnum):
    NONE = 0
    CONCEPTUAL = 1
    SECTOR = 2
    CANDIDATES = 3
    POST_MORTEM = 4


TIER_NAMES = {1: "conceptual_orientation", 2: "sector_localization", 3: "candidate_comparison", 4: "guided_post_mortem"}


class HintDenied(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HintLadder:
    tier: int = 0
    attempts: int = 0  # failed attempts on this position
    attempts_at_last_hint: int = 0
    forfeited: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "HintLadder":
        return cls(**d) if d else cls()

    def to_dict(self) -> dict:
        return asdict(self)

    def record_attempt(self) -> None:
        self.attempts += 1

    def can_advance(self, forfeit: bool = False) -> tuple[bool, str]:
        nxt = self.tier + 1
        if nxt > Tier.POST_MORTEM:
            return False, "ladder_exhausted"
        if self.attempts - self.attempts_at_last_hint < 1 and not (forfeit and nxt == Tier.POST_MORTEM):
            return False, "attempt_required"
        if nxt == Tier.POST_MORTEM and not (self.attempts >= 3 or forfeit):
            return False, "post_mortem_locked"
        return True, "ok"

    def advance(self, requested_tier: int | None = None, forfeit: bool = False) -> int:
        nxt = self.tier + 1
        if requested_tier is not None and requested_tier != nxt:
            raise HintDenied("tier_skip", f"Hints escalate one tier at a time; the next available tier is {nxt}.")
        ok, why = self.can_advance(forfeit)
        if not ok:
            msg = {
                "attempt_required": "Make an attempt (a move or a stated plan) before the next hint.",
                "post_mortem_locked": "The guided post-mortem opens after three failed attempts or an explicit forfeit.",
                "ladder_exhausted": "You are already in the guided post-mortem.",
            }[why]
            raise HintDenied(why, msg)
        self.tier = nxt
        self.attempts_at_last_hint = self.attempts
        self.forfeited = self.forfeited or forfeit
        return self.tier
