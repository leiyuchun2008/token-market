"""路由依赖：数据库、New API 客户端、当前登录用户与角色检查。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from .config import Settings
from .db import Database
from .errors import ApiError, forbidden
from .newapi import NewApi
from .security import parse_jwt


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_newapi(request: Request) -> NewApi:
    return request.app.state.newapi


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@dataclass
class User:
    id: str
    role: str          # buyer | seller | both | admin
    status: str
    nickname: str
    email: str | None
    phone: str | None
    credit_score: int
    newapi_user_id: int | None
    is_demo: bool
    row: dict

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_seller(self) -> bool:
        return self.role in ("seller", "both", "admin")

    @property
    def is_buyer(self) -> bool:
        return self.role in ("buyer", "both", "admin")


def to_user(row: dict) -> User:
    return User(id=str(row["id"]), role=row["role"], status=row["status"], nickname=row["nickname"], email=row.get("email"), phone=row.get("phone"),
                credit_score=int(row.get("credit_score") or 0), newapi_user_id=row.get("newapi_user_id"), is_demo=bool(row.get("is_demo")), row=row)


async def current_user(db: Annotated[Database, Depends(get_db)], authorization: Annotated[str | None, Header()] = None) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "请先登录")
    claims = parse_jwt(authorization[7:].strip())
    if not claims:
        raise ApiError(401, "unauthorized", "登录已过期，请重新登录")
    row = await db.fetch_one("SELECT * FROM users WHERE id = %s", (claims["sub"],))
    if not row:
        raise ApiError(401, "unauthorized", "账号不存在，请重新登录")
    u = to_user(row)
    if u.status in ("suspended", "banned"):
        raise ApiError(403, "suspended", "账号已被停用，请联系平台")
    return u


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise forbidden("只有平台管理员能做这个操作")
    return user


async def require_seller(user: CurrentUser) -> User:
    if not user.is_seller:
        raise ApiError(403, "not_seller", "你还不是卖方：先提交入驻申请，审核通过后再来")
    return user


async def require_buyer(user: CurrentUser) -> User:
    if not user.is_buyer:
        raise forbidden("这个账号不是买方账号")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
SellerUser = Annotated[User, Depends(require_seller)]
BuyerUser = Annotated[User, Depends(require_buyer)]
Db = Annotated[Database, Depends(get_db)]
Gw = Annotated[NewApi, Depends(get_newapi)]
Cfg = Annotated[Settings, Depends(get_settings)]
