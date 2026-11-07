"""应用入口。uvicorn app.main:app"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, settings as default_settings
from .db import Database, migrate
from .errors import install_handlers
from .jobs import run_jobs
from .newapi import NewApi
from .routers import admin, auth, buyer, public, seller

log = logging.getLogger("token-market")


def create_app(cfg: Settings | None = None, *, newapi_client: httpx.AsyncClient | None = None, run_migrations: bool = True, jobs: bool | None = None) -> FastAPI:
    cfg = cfg or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_migrations:
            applied = await migrate(cfg.database_url)
            if applied:
                log.info("migrations applied: %s", ", ".join(applied))
        app.state.db = Database(cfg.database_url)
        await app.state.db.open()
        app.state.newapi = NewApi(cfg.newapi_base_url, cfg.newapi_token, client=newapi_client)
        app.state.settings = cfg
        task = None
        if (cfg.jobs_enabled if jobs is None else jobs):
            task = asyncio.create_task(run_jobs(app.state.db, app.state.newapi))
        try:
            yield
        finally:
            if task:
                task.cancel()
            await app.state.newapi.aclose()
            await app.state.db.close()

    app = FastAPI(title="Token/API 算力交易平台", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    if cfg.cors_list:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_list, allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
    install_handlers(app)
    for r in (auth.router, buyer.router, seller.router, admin.router, public.router):
        app.include_router(r)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True}

    return app


app = create_app()
