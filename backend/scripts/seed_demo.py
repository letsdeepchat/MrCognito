"""Seed 30 days of plausible metrics for a demo user so the dashboard has something to show.

    python -m scripts.seed_demo   # prints a bearer token to paste into localStorage['cognito.token']
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import secrets
from datetime import timedelta

from app import db
from app.clock import utcnow
from app.db import CognitiveProfile, DailyMetric, User


async def main() -> None:
    db.init_engine()
    await db.create_all()
    rng = random.Random(3)
    token = secrets.token_urlsafe(32)
    async with db.sessionmaker()() as s:
        u = User(display_name="demo", token_hash=hashlib.sha256(token.encode()).hexdigest())
        s.add(u)
        await s.flush()
        s.add(CognitiveProfile(user_id=u.id, wellness_acknowledged_at=utcnow()))
        today = utcnow().date()
        for i in range(30, 0, -1):
            if rng.random() < 0.2:
                continue
            t = (30 - i) / 30
            s.add(DailyMetric(user_id=u.id, day=today - timedelta(days=i), budget_minutes=20,
                              active_minutes=round(rng.uniform(14, 20), 1),
                              working_memory_score=round(min(1, 0.55 + 0.25 * t + rng.gauss(0, .05)), 3),
                              calculation_depth_index=round(2.0 + 1.6 * t + rng.gauss(0, .3), 2),
                              trap_resistance_ratio=round(min(1, 0.5 + 0.3 * t + rng.gauss(0, .07)), 3),
                              reasoning_alignment_score=round(min(1, 0.6 + 0.2 * t + rng.gauss(0, .05)), 3),
                              adaptability_index=round(min(1, 0.5 + 0.3 * t + rng.gauss(0, .08)), 3)))
        await s.commit()
    print(token)


if __name__ == "__main__":
    asyncio.run(main())
