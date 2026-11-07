"""卖方接口：入驻、Key 托管、挂单、收益、结算、提现。返回里永远没有买方 / 子密钥的 id。"""
from __future__ import annotations

from decimal import Decimal

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import plain
from ..deps import CurrentUser, Db, Gw, User
from ..errors import ApiError, bad
from ..services import keys, sellers, settlements, usage, wallet

router = APIRouter(prefix="/api/seller", tags=["seller"])


async def active_seller(user: CurrentUser, db: Db) -> User:
    if not await sellers.is_active_seller(db, user.id):
        raise ApiError(403, "not_seller", "你还不是卖方：先提交入驻申请，审核通过后再来")
    return user


ActiveSeller = Annotated[User, Depends(active_seller)]


class ApplyIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=60)
    key_source_note: str = Field(min_length=10, max_length=2000)
    payout_method: dict | None = None


class ProfilePatch(BaseModel):
    display_name: str | None = None
    payout_method: dict | None = None


class KeyIn(BaseModel):
    provider_id: str
    secret: str = Field(min_length=8, max_length=1024)
    label: str | None = Field(default=None, max_length=80)
    base_url_override: str | None = None
    daily_cap_usd: Decimal | None = None
    extra_auth: dict | None = None


class KeyPatch(BaseModel):
    label: str | None = None
    daily_cap_usd: Decimal | None = None
    status: str | None = None


class ListingIn(BaseModel):
    key_id: str
    model_id: str
    settle_discount: Decimal


class ListingPatch(BaseModel):
    settle_discount: Decimal | None = None
    status: str | None = None


class PayoutIn(BaseModel):
    amount: Decimal


@router.post("/apply", status_code=201)
async def apply(body: ApplyIn, user: CurrentUser, db: Db):
    return plain(await sellers.apply(db, user.row, display_name=body.display_name, key_source_note=body.key_source_note, payout_method=body.payout_method))


@router.get("/profile")
async def get_profile(user: CurrentUser, db: Db):
    return plain(await sellers.profile(db, user.id))


@router.patch("/profile")
async def patch_profile(body: ProfilePatch, user: ActiveSeller, db: Db):
    return plain(await sellers.update_profile(db, user.id, display_name=body.display_name, payout_method=body.payout_method))


@router.get("/keys")
async def list_keys(user: ActiveSeller, db: Db):
    return plain(await keys.list_keys(db, user.id))


@router.post("/keys", status_code=202)
async def add_key(body: KeyIn, user: ActiveSeller, db: Db, gw: Gw):
    return plain(await keys.add_key(db, gw, user.row, provider_id=body.provider_id, secret=body.secret, label=body.label,
                                    base_url_override=body.base_url_override, daily_cap_usd=body.daily_cap_usd, extra_auth=body.extra_auth))


@router.get("/keys/{key_id}")
async def key_detail(key_id: str, user: ActiveSeller, db: Db):
    return plain(await keys.key_detail(db, key_id, user.id))


@router.patch("/keys/{key_id}")
async def patch_key(key_id: str, body: KeyPatch, user: ActiveSeller, db: Db, gw: Gw):
    if body.status is not None:
        return plain(await keys.set_key_status(db, gw, key_id, body.status, seller_id=user.id))
    return plain(await keys.update_key(db, key_id, user.id, label=body.label, daily_cap_usd=body.daily_cap_usd if "daily_cap_usd" in body.model_fields_set else ...))


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(key_id: str, user: ActiveSeller, db: Db, gw: Gw):
    await keys.set_key_status(db, gw, key_id, "removed", seller_id=user.id)
    return None


@router.post("/keys/{key_id}/revalidate")
async def revalidate(key_id: str, user: ActiveSeller, db: Db, gw: Gw):
    return plain(await keys.revalidate(db, gw, key_id, user.id))


@router.get("/listings")
async def list_listings(user: ActiveSeller, db: Db):
    return plain(await keys.list_listings(db, user.id))


@router.post("/listings", status_code=201)
async def add_listing(body: ListingIn, user: ActiveSeller, db: Db, gw: Gw):
    return plain(await keys.add_listing(db, gw, user.id, key_id=body.key_id, model_id=body.model_id, settle_discount=body.settle_discount))


@router.patch("/listings/{listing_id}")
async def patch_listing(listing_id: str, body: ListingPatch, user: ActiveSeller, db: Db, gw: Gw):
    if body.settle_discount is None and body.status is None:
        raise bad("没有要改的内容")
    return plain(await keys.update_listing(db, gw, user.id, listing_id, settle_discount=body.settle_discount, status=body.status))


@router.delete("/listings/{listing_id}", status_code=204)
async def delete_listing(listing_id: str, user: ActiveSeller, db: Db, gw: Gw):
    await keys.update_listing(db, gw, user.id, listing_id, status="removed")
    return None


@router.get("/market-hint")
async def market_hint(user: ActiveSeller, db: Db, model_id: str = Query(...)):
    return plain(await keys.market_hint(db, model_id))


@router.get("/earnings")
async def earnings(user: ActiveSeller, db: Db):
    return plain(await usage.seller_earnings(db, user.id))


@router.get("/usage")
async def seller_usage(user: ActiveSeller, db: Db, days: int = Query(30, ge=1, le=365)):
    return plain(await usage.seller_usage(db, user.id, days))


@router.get("/settlements")
async def list_settlements(user: ActiveSeller, db: Db):
    return plain(await settlements.list_settlements(db, seller_id=user.id))


@router.get("/settlements/{settlement_id}")
async def settlement_detail(settlement_id: str, user: ActiveSeller, db: Db):
    return plain(await settlements.settlement_detail(db, settlement_id, user.id))


@router.get("/payouts")
async def list_payouts(user: ActiveSeller, db: Db):
    return plain(await settlements.list_payouts(db, seller_id=user.id))


@router.post("/payouts", status_code=201)
async def request_payout(body: PayoutIn, user: ActiveSeller, db: Db):
    return plain(await settlements.request_payout(db, user.id, body.amount))


@router.get("/deposit")
async def deposit(user: ActiveSeller, db: Db):
    d = await sellers.deposit_summary(db, user.id)
    d["transactions"] = [t for t in await wallet.transactions(db, user.id, 100) if t["type"] in ("deposit_in", "deposit_out")]
    return plain(d)


@router.get("/demands")
async def open_demands(user: ActiveSeller, db: Db):
    from ..services import public
    return plain(await public.demands_with_band(db, 50))
