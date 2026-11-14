"""The five-phase daily protocol and its proportional time budget."""
from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    BLINDFOLD_WARMUP = "BLINDFOLD_WARMUP"
    PATTERN_INHIBITION = "PATTERN_INHIBITION"
    SPARRING_VERBALIZED = "SPARRING_VERBALIZED"
    ADAPTABILITY_PERTURBATION = "ADAPTABILITY_PERTURBATION"
    RECALL_CONSOLIDATION = "RECALL_CONSOLIDATION"


ORDER = list(Phase)
WEIGHTS = {
    Phase.BLINDFOLD_WARMUP: 0.15,
    Phase.PATTERN_INHIBITION: 0.20,
    Phase.SPARRING_VERBALIZED: 0.35,
    Phase.ADAPTABILITY_PERTURBATION: 0.15,
    Phase.RECALL_CONSOLIDATION: 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

COGNITIVE_TARGET = {
    Phase.BLINDFOLD_WARMUP: "visuospatial working memory",
    Phase.PATTERN_INHIBITION: "pattern recognition and impulse inhibition",
    Phase.SPARRING_VERBALIZED: "verbal reasoning and executive planning",
    Phase.ADAPTABILITY_PERTURBATION: "cognitive flexibility",
    Phase.RECALL_CONSOLIDATION: "episodic memory and retrieval",
}

# Items per phase by daily budget (10 / 20 / 30 minutes).
ITEMS = {
    Phase.BLINDFOLD_WARMUP: {10: 1, 20: 2, 30: 3},
    Phase.PATTERN_INHIBITION: {10: 2, 20: 3, 30: 4},
    Phase.SPARRING_VERBALIZED: {10: 4, 20: 6, 30: 8},  # user moves
    Phase.ADAPTABILITY_PERTURBATION: {10: 1, 20: 2, 30: 3},
    Phase.RECALL_CONSOLIDATION: {10: 1, 20: 1, 30: 1},
}


def phase_seconds(budget_minutes: int, phase: Phase) -> int:
    return int(round(budget_minutes * 60 * WEIGHTS[phase]))


def next_phase(p: Phase) -> Phase | None:
    i = ORDER.index(p)
    return ORDER[i + 1] if i + 1 < len(ORDER) else None


def items_for(phase: Phase, budget: int) -> int:
    return ITEMS[phase].get(budget, ITEMS[phase][20])
