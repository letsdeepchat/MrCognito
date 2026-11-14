"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .api.routes import router
from .config import get_settings
from .engines import registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_engine()
    await db.create_all()
    yield
    await registry.shutdown()
    await db.dispose()


def create_app(use_lifespan: bool = True) -> FastAPI:
    app = FastAPI(title="Cognitive-Reserve Chess Assistant", version="0.1.0", lifespan=lifespan if use_lifespan else None)
    app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","), allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)
    return app


app = create_app()
