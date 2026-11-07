"""账户：注册、登录、在转发引擎里对应的用户（买方额度住在那里）。"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

from ..db import Database
from ..errors import ApiError, bad
from ..newapi import Credential, NewApi, NewApiError
from ..security import hash_password, make_jwt, random_password, seal, unseal, verify_password
from . import feed

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{6,20}$")


def public(row: dict) -> dict:
    return {"id": str(row["id"]), "role": row["role"], "status": row["status"], "nickname": row["nickname"], "email": row.get("email"),
            "phone": row.get("phone"), "credit_score": row.get("credit_score"), "is_demo": row.get("is_demo"), "created_at": row.get("created_at")}


async def register(db: Database, gw: NewApi, *, nickname: str, password: str, email: str | None = None, phone: str | None = None,
                   role: str = "buyer", key_source_note: str | None = None) -> dict:
    nickname = (nickname or "").strip()
    if not 2 <= len(nickname) <= 40:
        raise bad("昵称要 2 到 40 个字", "nickname")
    if len(password or "") < 8:
        raise bad("密码至少 8 位", "password")
    email = (email or "").strip().lower() or None
    phone = re.sub(r"[\s-]", "", phone or "") or None
    if not email and not phone:
        raise bad("邮箱和手机号至少填一个", "email")
    if email and not EMAIL_RE.match(email):
        raise bad("邮箱格式不对", "email")
    if phone and not PHONE_RE.match(phone):
        raise bad("手机号格式不对", "phone")
    if role not in ("buyer", "seller", "both"):
        raise bad("角色只能是 buyer / seller / both", "role")
    if email and await db.fetch_one("SELECT 1 FROM users WHERE email = %s", (email,)):
        raise ApiError(409, "exists", "这个邮箱已经注册过了", "email")
    if phone and await db.fetch_one("SELECT 1 FROM users WHERE phone = %s", (phone,)):
        raise ApiError(409, "exists", "这个手机号已经注册过了", "phone")
    # 想做卖方的先以买方身份进来，入驻申请走审核（审核通过才变成 seller/both）
    async with db.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO users (role, nickname, email, phone, password_hash) VALUES ('buyer', %s, %s, %s, %s) RETURNING *",
            (nickname, email, phone, hash_password(password)))
        row = await cur.fetchone()
        await conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (row["id"],))
        if role in ("seller", "both"):
            await conn.execute("INSERT INTO seller_profiles (user_id, display_name, key_source_note) VALUES (%s, %s, %s)",
                               (row["id"], nickname, key_source_note or ""))
    await feed.add(db, "buyer_joined", f"买家 {feed.mask(nickname)} 加入了平台")
    # 转发引擎里的用户：现在就建，建不成（引擎没配好）就等第一次用到时再建
    try:
        await ensure_newapi_user(db, gw, row)
    except NewApiError:
        pass
    row = await db.fetch_one("SELECT * FROM users WHERE id = %s", (row["id"],))
    return {"token": make_jwt(str(row["id"]), row["role"]), "user": public(row)}


async def login(db: Database, *, account: str, password: str) -> dict:
    account = (account or "").strip().lower()
    row = await db.fetch_one("SELECT * FROM users WHERE email = %s OR phone = %s", (account, account))
    if not row or not verify_password(password or "", row.get("password_hash")):
        raise ApiError(401, "bad_login", "账号或密码不对")
    if row["status"] in ("suspended", "banned"):
        raise ApiError(403, "suspended", "账号已被停用，请联系平台")
    return {"token": make_jwt(str(row["id"]), row["role"]), "user": public(row)}


async def ensure_newapi_user(db: Database, gw: NewApi, user: dict) -> int:
    """返回这个用户在转发引擎里的 id；没有就建一个（用户名 u+8 位，随机密码由平台加密保管）。"""
    if user.get("newapi_user_id"):
        return int(user["newapi_user_id"])
    if not gw.configured:
        raise NewApiError("转发引擎还没接入（NEWAPI_BASE_URL / NEWAPI_TOKEN）", 503)
    username = "u" + uuid.UUID(str(user["id"])).hex[:10]
    password = random_password()
    existing = await gw.search_user(username)
    if existing:
        # 之前建过但没记下来：换个密码我们记不住旧的，所以这里只能报错让人处理
        raise NewApiError(f"转发引擎里已经有用户 {username}，但平台没有它的密码；请在后台处理", 500)
    u = await gw.create_user(username, password, user["nickname"])
    await db.execute("UPDATE users SET newapi_user_id = %s, newapi_username = %s, newapi_password_ct = %s, updated_at = now() WHERE id = %s",
                     (int(u["id"]), username, seal(password), user["id"]))
    return int(u["id"])


@asynccontextmanager
async def newapi_session(db: Database, gw: NewApi, user: dict):
    """以买方自己的身份登录转发引擎（建/删令牌要用自己的会话），用完注销。"""
    await ensure_newapi_user(db, gw, user)
    row = await db.fetch_one("SELECT newapi_username, newapi_password_ct FROM users WHERE id = %s", (user["id"],))
    r = await gw.login(row["newapi_username"], unseal(row["newapi_password_ct"]))
    cred: Credential = r["session"]
    try:
        yield cred
    finally:
        await gw.sign_out(cred)
