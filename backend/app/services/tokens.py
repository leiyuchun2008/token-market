"""买方子密钥：在转发引擎里以买方自己的身份建令牌，平台只存 sha256 和前缀。"""
from __future__ import annotations

from decimal import Decimal

from ..db import Database
from ..errors import ApiError, bad, not_found
from ..newapi import NewApi, NewApiError
from ..security import sha256_hex
from . import users

TOKEN_PREFIX = "sk-"   # 买方拿到的完整密钥是 sk-<New API 的 key>


def public_token(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("token_hash", "newapi_token_id")}


async def list_tokens(db: Database, buyer_id: str) -> list[dict]:
    rows = await db.fetch_all("SELECT * FROM api_tokens WHERE buyer_id = %s AND status <> 'deleted' ORDER BY created_at DESC", (buyer_id,))
    return [public_token(r) for r in rows]


async def create_token(db: Database, gw: NewApi, buyer: dict, *, name: str, allowed_models: list[str] | None = None,
                       quota_limit: Decimal | None = None, rate_limit_rpm: int | None = None) -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= 60:
        raise bad("密钥名称 1 到 60 个字", "name")
    n = await db.fetch_val("SELECT COUNT(*) FROM api_tokens WHERE buyer_id = %s AND status <> 'deleted'", (buyer["id"],))
    if int(n or 0) >= 20:
        raise ApiError(400, "too_many", "一个账号最多 20 个子密钥")
    models = [m.strip() for m in (allowed_models or []) if m and m.strip()] or None
    if models:
        known = {r["id"] for r in await db.fetch_all("SELECT id FROM models WHERE id = ANY(%s)", (models,))}
        missing = [m for m in models if m not in known]
        if missing:
            raise bad(f"模型不存在：{', '.join(missing)}", "allowed_models")
    # 转发引擎里的令牌名加上后缀，避免买方重名导致找不到刚建的那个
    inner_name = f"{name[:40]}#{sha256_hex(str(buyer['id']) + name + str(n))[:6]}"
    async with users.newapi_session(db, gw, buyer) as cred:
        created = await gw.create_token(cred, inner_name, models=models)
    full = TOKEN_PREFIX + created["key"]
    row = await db.fetch_one(
        "INSERT INTO api_tokens (buyer_id, name, token_hash, token_prefix, allowed_models, quota_limit, rate_limit_rpm, newapi_token_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (buyer["id"], name, sha256_hex(full), full[:12], models, quota_limit, rate_limit_rpm, created["id"]))
    d = public_token(row)
    d["token"] = full   # 明文只在这里返回一次
    return d


async def get_token(db: Database, token_id: str, buyer_id: str) -> dict:
    row = await db.fetch_one("SELECT * FROM api_tokens WHERE id = %s AND buyer_id = %s AND status <> 'deleted'", (token_id, buyer_id))
    if not row:
        raise not_found("子密钥")
    return row


async def _newapi_token(gw: NewApi, cred, newapi_token_id: int) -> dict | None:
    for t in await gw.list_tokens(cred):
        if int(t.get("id") or 0) == int(newapi_token_id):
            return t
    return None


async def set_status(db: Database, gw: NewApi, buyer: dict, token_id: str, status: str) -> dict:
    if status not in ("active", "disabled"):
        raise bad("状态只能是 active / disabled", "status")
    row = await get_token(db, token_id, buyer["id"])
    if row.get("newapi_token_id"):
        async with users.newapi_session(db, gw, buyer) as cred:
            t = await _newapi_token(gw, cred, row["newapi_token_id"])
            if t:
                await gw.update_token(cred, t, status=1 if status == "active" else 2)
    await db.execute("UPDATE api_tokens SET status = %s WHERE id = %s", (status, token_id))
    return public_token(await get_token(db, token_id, buyer["id"]))


async def delete_token(db: Database, gw: NewApi, buyer: dict, token_id: str) -> None:
    row = await get_token(db, token_id, buyer["id"])
    if row.get("newapi_token_id"):
        try:
            async with users.newapi_session(db, gw, buyer) as cred:
                await gw.delete_token(cred, int(row["newapi_token_id"]))
        except NewApiError as e:
            if e.status >= 500:
                raise
    await db.execute("UPDATE api_tokens SET status = 'deleted' WHERE id = %s", (token_id,))
