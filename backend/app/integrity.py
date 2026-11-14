"""Pacing-integrity monitor (anti external-engine lookup).

Signal: over a sliding window of critical decisions, a very high top-1 engine
match rate combined with unnaturally *uniform* think times (low coefficient of
variation) is characteristic of copying moves from an engine. Humans show
bursty latency: fast on obvious moves, slow on hard ones.

On trigger the session is put on hold and switched to a blindfold
verification drill about the current position; passing it resumes play.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import get_settings


@dataclass
class IntegrityVerdict:
    suspicious: bool
    top1_rate: float
    latency_cv: float
    n: int


def evaluate(samples: list[list]) -> IntegrityVerdict:
    """samples: [think_time_ms, matched_top1 (0/1), is_hard (0/1)]"""
    s = get_settings()
    win = samples[-s.integrity_window:]
    if len(win) < s.integrity_window:
        return IntegrityVerdict(False, 0.0, 1.0, len(win))
    times = [max(1, w[0]) for w in win]
    top1 = sum(w[1] for w in win) / len(win)
    cv = statistics.pstdev(times) / statistics.mean(times)
    return IntegrityVerdict(top1 >= s.integrity_top1_threshold and cv <= s.integrity_latency_cv_threshold,
                            round(top1, 3), round(cv, 3), len(win))
