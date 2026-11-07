"""不用登录的接口：市场页、首页数字、动态、求购。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import plain
from ..deps import Db
from ..errors import not_found
from ..services import public

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/models")
async def models(db: Db, include_demo: bool = Query(True)):
    return plain(await public.market_models(db, include_demo))


@router.get("/models/{model_id}")
async def model(model_id: str, db: Db):
    m = await public.model_detail(db, model_id)
    if not m:
        raise not_found("模型")
    return plain(m)


@router.get("/models/{model_id}/health")
async def health(model_id: str, db: Db):
    return plain(await public.model_health(db, model_id))


@router.get("/stats")
async def stats(db: Db):
    return plain(await public.stats(db))


@router.get("/feed")
async def feed(db: Db, limit: int = Query(30, ge=1, le=200)):
    return plain(await public.recent_feed(db, limit))


@router.get("/demands")
async def demands(db: Db, limit: int = Query(30, ge=1, le=100)):
    return plain(await public.demands_with_band(db, limit))
