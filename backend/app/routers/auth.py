from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..db import plain
from ..deps import CurrentUser, Db, Gw
from ..services import sellers, users, wallet

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    nickname: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    phone: str | None = None
    role: str = "buyer"
    key_source_note: str | None = None
    invite_code: str | None = None


class LoginIn(BaseModel):
    account: str = Field(min_length=3, max_length=120, description="邮箱或手机号")
    password: str = Field(min_length=1, max_length=128)


@router.post("/register", status_code=201)
async def register(body: RegisterIn, db: Db, gw: Gw):
    return plain(await users.register(db, gw, nickname=body.nickname, password=body.password, email=body.email, phone=body.phone, role=body.role, key_source_note=body.key_source_note))


@router.post("/login")
async def login(body: LoginIn, db: Db):
    return plain(await users.login(db, account=body.account, password=body.password))


@router.get("/me")
async def me(user: CurrentUser, db: Db):
    w = await wallet.get_wallet(db, user.id)
    sp = await sellers.profile(db, user.id)
    return plain({"user": users.public(user.row), "wallet": {"balance": w["balance"], "frozen": w["frozen"], "pending_payout": w["pending_payout"], "currency": w["currency"]},
                  "seller_profile": sp, "is_admin": user.is_admin, "is_seller": user.is_seller and (user.is_admin or (sp and sp["review_status"] == "approved")),
                  "is_buyer": user.is_buyer})
