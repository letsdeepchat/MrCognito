"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os
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
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built React app from the same origin when COGNITO_STATIC_DIR is set (single-service deploys)."""
    static = get_settings().static_dir
    if not static or not os.path.isdir(static):
        return
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=os.path.join(static, "assets")), name="assets")
    index = os.path.join(static, "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        f = os.path.realpath(os.path.join(static, path))
        if path and not path.startswith("api") and f.startswith(os.path.realpath(static)) and os.path.isfile(f):
            return FileResponse(f)
        if path.startswith("api"):
            from fastapi import HTTPException

            raise HTTPException(404)
        return FileResponse(index)


app = create_app()
