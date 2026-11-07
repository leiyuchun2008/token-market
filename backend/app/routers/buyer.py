"""买方接口：子密钥、钱包、充值、用量、求购。返回里永远没有卖方 / Key / 挂单的 id。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..db import plain
from ..deps import BuyerUser, Cfg, Db, Gw
from ..errors import bad
from ..services import tokens, usage, wallet

router = APIRouter(prefix="/api/buyer", tags=["buyer"])


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    allowed_models: list[str] | None = None
    quota_limit: Decimal | None = None
    rate_limit_rpm: int | None = None


class TokenPatch(BaseModel):
    status: str | None = None


class TopupIn(BaseModel):
    amount: Decimal
    pay_method: str
    pay_proof_url: str | None = None


class DemandIn(BaseModel):
    model_id: str | None = None
    provider_id: str | None = None
    description: str = Field(min_length=5, max_length=2000)
    budget_cny: Decimal | None = None
    target_discount: Decimal | None = None
    expected_monthly_usd: Decimal | None = None


@router.get("/tokens")
async def list_tokens(user: BuyerUser, db: Db, cfg: Cfg):
    return plain({"items": await tokens.list_tokens(db, user.id), "api_base": cfg.public_api_base})


@router.post("/tokens", status_code=201)
async def create_token(body: TokenIn, user: BuyerUser, db: Db, gw: Gw, cfg: Cfg):
    out = await tokens.create_token(db, gw, user.row, name=body.name, allowed_models=body.allowed_models, quota_limit=body.quota_limit, rate_limit_rpm=body.rate_limit_rpm)
    out["api_base"] = cfg.public_api_base
    return plain(out)


@router.patch("/tokens/{token_id}")
async def patch_token(token_id: str, body: TokenPatch, user: BuyerUser, db: Db, gw: Gw):
    if body.status is None:
        raise bad("没有要改的内容")
    return plain(await tokens.set_status(db, gw, user.row, token_id, body.status))


@router.delete("/tokens/{token_id}", status_code=204)
async def delete_token(token_id: str, user: BuyerUser, db: Db, gw: Gw):
    await tokens.delete_token(db, gw, user.row, token_id)
    return None


@router.get("/wallet")
async def get_wallet(user: BuyerUser, db: Db):
    return plain(await wallet.get_wallet(db, user.id))


@router.get("/transactions")
async def transactions(user: BuyerUser, db: Db, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return plain(await wallet.transactions(db, user.id, limit, offset))


@router.get("/topups")
async def list_topups(user: BuyerUser, db: Db):
    return plain(await wallet.list_topups(db, buyer_id=user.id))


@router.post("/topups", status_code=201)
async def create_topup(body: TopupIn, user: BuyerUser, db: Db):
    return plain(await wallet.create_topup(db, user.row, amount=body.amount, pay_method=body.pay_method, pay_proof_url=body.pay_proof_url))


@router.post("/topups/{topup_id}/cancel")
async def cancel_topup(topup_id: str, user: BuyerUser, db: Db):
    return plain(await wallet.cancel_topup(db, user.id, topup_id))


@router.get("/usage")
async def buyer_usage(user: BuyerUser, db: Db, days: int = Query(30, ge=1, le=365), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return plain(await usage.buyer_usage(db, user.id, days, limit, offset))


@router.get("/usage/summary")
async def buyer_summary(user: BuyerUser, db: Db, days: int = Query(30, ge=1, le=365)):
    return plain(await usage.buyer_summary(db, user.id, days))


@router.post("/demands", status_code=201)
async def post_demand(body: DemandIn, user: BuyerUser, db: Db):
    if body.model_id and not await db.fetch_one("SELECT 1 FROM models WHERE id = %s", (body.model_id,)):
        raise bad("模型不存在", "model_id")
    row = await db.fetch_one(
        "INSERT INTO demand_requests (buyer_id, model_id, provider_id, description, budget_cny, target_discount, expected_monthly_usd) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (user.id, body.model_id, body.provider_id, body.description, body.budget_cny, body.target_discount, body.expected_monthly_usd))
    from ..services import feed
    await feed.add(db, "demand_posted", f"买家 {feed.mask(user.nickname)} 发布了求购：{(body.model_id or body.provider_id or '算力资源')}", model_id=body.model_id)
    return plain(row)


@router.get("/demands")
async def my_demands(user: BuyerUser, db: Db):
    return plain(await db.fetch_all("SELECT * FROM demand_requests WHERE buyer_id = %s ORDER BY created_at DESC", (user.id,)))
