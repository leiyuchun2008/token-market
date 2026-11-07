"""卖方入驻：申请、审核、资料。审核通过后用户角色变成 seller / both。"""
from __future__ import annotations

from decimal import Decimal

from ..db import Database, jsonb
from ..errors import ApiError, bad, not_found
from ..security import seal, unseal
from . import configs, feed

PAYOUT_METHODS = ("bank", "alipay", "wechat", "usdt")


def _check_payout(method: dict | None) -> dict | None:
    if method is None:
        return None
    if not isinstance(method, dict) or method.get("type") not in PAYOUT_METHODS:
        raise bad("收款方式要包含 type（bank / alipay / wechat / usdt）", "payout_method")
    out = {k: str(v)[:120] for k, v in method.items() if v is not None}
    if not out.get("account"):
        raise bad("收款方式要有 account（卡号 / 账号 / 钱包地址）", "payout_method.account")
    return out


async def profile(db: Database, user_id: str) -> dict | None:
    row = await db.fetch_one("SELECT p.*, u.role FROM seller_profiles p JOIN users u ON u.id = p.user_id WHERE p.user_id = %s", (user_id,))
    if not row:
        return None
    out = dict(row)
    pm = out.pop("payout_method", None)
    role = out.pop("role")
    out["payout_method"] = _masked_payout(pm)
    out["review_status"] = "pending" if row["reviewed_at"] is None else ("approved" if role in ("seller", "both", "admin") else "rejected")
    return out


def _masked_payout(pm) -> dict | None:
    if not pm:
        return None
    try:
        data = pm if isinstance(pm, dict) else None
        if data and "ct" in data:
            import json
            data = json.loads(unseal(bytes.fromhex(data["ct"])))
    except Exception:
        return {"type": "unknown"}
    if not data:
        return None
    acct = str(data.get("account", ""))
    return {**data, "account": acct[:3] + "****" + acct[-4:] if len(acct) > 8 else "****"}


def _sealed_payout(method: dict | None):
    if method is None:
        return None
    import json
    return jsonb({"type": method["type"], "ct": seal(json.dumps(method, ensure_ascii=False)).hex()})


async def apply(db: Database, user: dict, *, display_name: str | None, key_source_note: str, payout_method: dict | None) -> dict:
    """提交入驻申请；已经是卖方的改成更新资料。"""
    note = (key_source_note or "").strip()
    if len(note) < 10:
        raise bad("请写清楚 Key 的来源与可供应的模型（至少 10 个字）", "key_source_note")
    name = (display_name or user["nickname"]).strip()[:60]
    pm = _check_payout(payout_method)
    existing = await db.fetch_one("SELECT * FROM seller_profiles WHERE user_id = %s", (user["id"],))
    if existing and existing["reviewed_at"] is None:
        await db.execute("UPDATE seller_profiles SET display_name = %s, key_source_note = %s, payout_method = COALESCE(%s, payout_method) WHERE user_id = %s",
                         (name, note, _sealed_payout(pm), user["id"]))
    elif existing:
        if user["role"] not in ("seller", "both", "admin"):
            # 之前被驳回：重新进入待审
            await db.execute("UPDATE seller_profiles SET display_name = %s, key_source_note = %s, payout_method = COALESCE(%s, payout_method), reviewed_at = NULL, reviewed_by = NULL WHERE user_id = %s",
                             (name, note, _sealed_payout(pm), user["id"]))
        else:
            await db.execute("UPDATE seller_profiles SET display_name = %s, key_source_note = %s, payout_method = COALESCE(%s, payout_method) WHERE user_id = %s",
                             (name, note, _sealed_payout(pm), user["id"]))
    else:
        cycle = int(await configs.num(db, "default_settlement_cycle_days"))
        await db.execute("INSERT INTO seller_profiles (user_id, display_name, settlement_cycle_days, key_source_note, payout_method) VALUES (%s, %s, %s, %s, %s)",
                         (user["id"], name, cycle, note, _sealed_payout(pm)))
    return await profile(db, user["id"])


async def update_profile(db: Database, user_id: str, *, display_name: str | None = None, payout_method: dict | None = None) -> dict:
    if display_name is not None:
        if not 2 <= len(display_name.strip()) <= 60:
            raise bad("店铺名 2 到 60 个字", "display_name")
        await db.execute("UPDATE seller_profiles SET display_name = %s WHERE user_id = %s", (display_name.strip(), user_id))
    if payout_method is not None:
        await db.execute("UPDATE seller_profiles SET payout_method = %s WHERE user_id = %s", (_sealed_payout(_check_payout(payout_method)), user_id))
    return await profile(db, user_id)


async def payout_method_plain(db: Database, user_id: str) -> dict | None:
    """给后台打款看的完整收款信息（解密）。"""
    row = await db.fetch_one("SELECT payout_method FROM seller_profiles WHERE user_id = %s", (user_id,))
    pm = row and row["payout_method"]
    if not pm or "ct" not in pm:
        return None
    import json
    return json.loads(unseal(bytes.fromhex(pm["ct"])))


async def pending(db: Database) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT p.user_id, p.display_name, p.key_source_note, p.created_at, u.nickname, u.email, u.phone, u.role, u.credit_score "
        "FROM seller_profiles p JOIN users u ON u.id = p.user_id WHERE p.reviewed_at IS NULL ORDER BY p.created_at")
    return rows


async def review(db: Database, admin_id: str, seller_user_id: str, *, approve: bool, deposit_required: Decimal | None = None,
                 settlement_cycle_days: int | None = None, note: str | None = None) -> dict:
    row = await db.fetch_one("SELECT p.*, u.role FROM seller_profiles p JOIN users u ON u.id = p.user_id WHERE p.user_id = %s", (seller_user_id,))
    if not row:
        raise not_found("入驻申请")
    async with db.connection() as conn:
        if approve:
            new_role = "both" if row["role"] in ("buyer", "both") else "seller"
            await conn.execute("UPDATE users SET role = %s, updated_at = now() WHERE id = %s", (new_role, seller_user_id))
            await conn.execute(
                "UPDATE seller_profiles SET reviewed_by = %s, reviewed_at = now(), deposit_required = COALESCE(%s, deposit_required), "
                "settlement_cycle_days = COALESCE(%s, settlement_cycle_days) WHERE user_id = %s",
                (admin_id, deposit_required, settlement_cycle_days, seller_user_id))
        else:
            await conn.execute("UPDATE seller_profiles SET reviewed_by = %s, reviewed_at = now() WHERE user_id = %s", (admin_id, seller_user_id))
            await conn.execute("UPDATE users SET role = CASE WHEN role = 'both' THEN 'buyer' ELSE role END, updated_at = now() WHERE id = %s", (seller_user_id,))
        await conn.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, %s, 'seller', %s, %s)",
                           (admin_id, "seller.approve" if approve else "seller.reject", seller_user_id, jsonb({"note": note})))
    if approve:
        await feed.add(db, "seller_joined", f"供应商 {feed.mask(row['display_name'] or '')} 入驻了平台")
    return await profile(db, seller_user_id)


async def is_active_seller(db: Database, user_id: str) -> bool:
    row = await db.fetch_one("SELECT u.role, p.reviewed_at FROM users u LEFT JOIN seller_profiles p ON p.user_id = u.id WHERE u.id = %s", (user_id,))
    return bool(row and row["role"] in ("seller", "both", "admin") and (row["role"] == "admin" or row["reviewed_at"]))


async def deposit_summary(db: Database, user_id: str) -> dict:
    row = await db.fetch_one("SELECT deposit_required, deposit_balance, settlement_cycle_days FROM seller_profiles WHERE user_id = %s", (user_id,))
    return dict(row) if row else {"deposit_required": 0, "deposit_balance": 0, "settlement_cycle_days": 7}


async def record_deposit(db: Database, admin_id: str, seller_user_id: str, amount: Decimal, note: str | None) -> dict:
    """后台登记保证金（正数缴纳、负数退还/扣罚）。"""
    if amount == 0:
        raise bad("金额不能为 0", "amount")
    async with db.connection() as conn:
        cur = await conn.execute("UPDATE seller_profiles SET deposit_balance = deposit_balance + %s WHERE user_id = %s RETURNING deposit_balance", (amount, seller_user_id))
        r = await cur.fetchone()
        if not r:
            raise not_found("卖方")
        if r["deposit_balance"] < 0:
            raise ApiError(400, "deposit_negative", "退还金额超过了保证金余额")
        cur = await conn.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (seller_user_id,))
        w = await cur.fetchone()
        await conn.execute(
            "INSERT INTO wallet_transactions (user_id, type, amount, balance_after, ref_type, note) VALUES (%s, %s, %s, %s, 'deposit', %s)",
            (seller_user_id, "deposit_in" if amount > 0 else "deposit_out", amount, w["balance"] if w else 0, note))
        await conn.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'seller.deposit', 'seller', %s, %s)",
                           (admin_id, seller_user_id, jsonb({"amount": str(amount), "note": note})))
    return await deposit_summary(db, seller_user_id)
