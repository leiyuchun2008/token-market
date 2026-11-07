"""钱包与充值。

平台的 wallets 是账本：充值审核通过 → 余额增加，同时给买方在转发引擎里的用户加等值额度；
调用消费由 usage.sync 从引擎日志同步回来扣余额。余额和引擎额度偶有时间差，后台「对账」会校正。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..db import Database, jsonb
from ..errors import ApiError, bad, not_found
from ..newapi import NewApi, NewApiError
from . import configs, feed, users

PAY_METHODS = ("bank", "alipay", "wechat", "usdt")
Q2 = Decimal("0.01")


async def get_wallet(db: Database, user_id: str) -> dict:
    row = await db.fetch_one("SELECT * FROM wallets WHERE user_id = %s", (user_id,))
    if not row:
        await db.execute("INSERT INTO wallets (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        row = await db.fetch_one("SELECT * FROM wallets WHERE user_id = %s", (user_id,))
    return dict(row)


async def transactions(db: Database, user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    return await db.fetch_all("SELECT id, type, amount, balance_after, ref_type, ref_id, note, is_demo, created_at FROM wallet_transactions WHERE user_id = %s ORDER BY id DESC LIMIT %s OFFSET %s",
                              (user_id, max(1, min(200, limit)), max(0, offset)))


async def post_transaction(conn, user_id: str, kind: str, amount: Decimal, *, ref_type: str | None = None, ref_id: str | None = None,
                           note: str | None = None, allow_negative: bool = False, is_demo: bool = False) -> Decimal:
    """在同一事务里改余额并记流水（行锁 + 乐观版本号）。返回新余额。"""
    cur = await conn.execute("SELECT balance, version FROM wallets WHERE user_id = %s FOR UPDATE", (user_id,))
    w = await cur.fetchone()
    if not w:
        await conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (user_id,))
        w = {"balance": Decimal(0), "version": 0}
    new_balance = Decimal(str(w["balance"])) + amount
    if new_balance < 0 and not allow_negative:
        raise ApiError(402, "insufficient_balance", "余额不足")
    await conn.execute("UPDATE wallets SET balance = %s, version = version + 1, updated_at = now() WHERE user_id = %s", (new_balance, user_id))
    await conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, balance_after, ref_type, ref_id, note, is_demo) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                       (user_id, kind, amount, new_balance, ref_type, ref_id, note, is_demo))
    return new_balance


async def cny_to_quota(db: Database, cny: Decimal) -> int:
    fx = await configs.dec(db, "fx_usd_cny")
    qpu = Decimal(str(await configs.get(db, "newapi_quota_per_unit")))
    return int((cny / fx * qpu).to_integral_value(rounding=ROUND_HALF_UP))


async def quota_to_cny(db: Database, quota: int | Decimal) -> Decimal:
    fx = await configs.dec(db, "fx_usd_cny")
    qpu = Decimal(str(await configs.get(db, "newapi_quota_per_unit")))
    return (Decimal(quota) / qpu * fx)


# ------------------------------------------------------------------ 充值单
async def list_topups(db: Database, buyer_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT t.*, u.nickname FROM topup_orders t JOIN users u ON u.id = t.buyer_id WHERE TRUE"
    params: list = []
    if buyer_id:
        sql += " AND t.buyer_id = %s"
        params.append(buyer_id)
    if status:
        sql += " AND t.status = %s"
        params.append(status)
    sql += " ORDER BY t.created_at DESC LIMIT %s"
    params.append(max(1, min(200, limit)))
    return await db.fetch_all(sql, params)


async def create_topup(db: Database, buyer: dict, *, amount: Decimal, pay_method: str, pay_proof_url: str | None = None) -> dict:
    if amount < Decimal("10") or amount > Decimal("10000000"):
        raise bad("单笔充值 10 元起", "amount")
    if pay_method not in PAY_METHODS:
        raise bad("支付方式只能是 bank / alipay / wechat / usdt", "pay_method")
    pending = await db.fetch_val("SELECT COUNT(*) FROM topup_orders WHERE buyer_id = %s AND status = 'pending'", (buyer["id"],))
    if int(pending or 0) >= 5:
        raise ApiError(400, "too_many_pending", "有 5 笔充值还在审核中，请等审核完再提交")
    first = not await db.fetch_one("SELECT 1 FROM topup_orders WHERE buyer_id = %s AND status = 'approved'", (buyer["id"],))
    bonus = (amount * await configs.dec(db, "first_topup_bonus_rate")).quantize(Q2) if first else Decimal(0)
    row = await db.fetch_one(
        "INSERT INTO topup_orders (buyer_id, amount, bonus, pay_method, pay_proof_url) VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (buyer["id"], amount.quantize(Q2), bonus, pay_method, (pay_proof_url or None)))
    return row


async def cancel_topup(db: Database, buyer_id: str, topup_id: str) -> dict:
    row = await db.fetch_one("UPDATE topup_orders SET status = 'cancelled' WHERE id = %s AND buyer_id = %s AND status = 'pending' RETURNING *", (topup_id, buyer_id))
    if not row:
        raise not_found("待审核的充值单")
    return row


async def review_topup(db: Database, gw: NewApi, admin_id: str, topup_id: str, *, approve: bool, note: str | None = None) -> dict:
    row = await db.fetch_one("SELECT * FROM topup_orders WHERE id = %s", (topup_id,))
    if not row:
        raise not_found("充值单")
    if row["status"] != "pending":
        raise ApiError(409, "already_reviewed", "这笔充值已经处理过了")
    if not approve:
        await db.execute("UPDATE topup_orders SET status = 'rejected', reviewed_by = %s, reviewed_at = now() WHERE id = %s", (admin_id, topup_id))
        await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'topup.reject', 'topup_order', %s, %s)",
                         (admin_id, topup_id, jsonb({"note": note})))
        return await db.fetch_one("SELECT * FROM topup_orders WHERE id = %s", (topup_id,))
    amount = Decimal(str(row["amount"]))
    bonus = Decimal(str(row["bonus"] or 0))
    buyer = await db.fetch_one("SELECT * FROM users WHERE id = %s", (row["buyer_id"],))
    # 先给引擎加额度（失败就不入账，保持两边一致）
    quota = await cny_to_quota(db, amount + bonus)
    if gw.configured:
        uid = await users.ensure_newapi_user(db, gw, buyer)
        await gw.add_quota(uid, quota, "add")
    async with db.connection() as conn:
        await post_transaction(conn, str(row["buyer_id"]), "topup", amount, ref_type="topup_order", ref_id=str(topup_id), note=f"{row['pay_method']} 充值")
        if bonus > 0:
            await post_transaction(conn, str(row["buyer_id"]), "bonus", bonus, ref_type="topup_order", ref_id=str(topup_id), note="首充赠送")
        await conn.execute("UPDATE topup_orders SET status = 'approved', reviewed_by = %s, reviewed_at = now() WHERE id = %s", (admin_id, topup_id))
        await conn.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'topup.approve', 'topup_order', %s, %s)",
                           (admin_id, topup_id, jsonb({"amount": str(amount), "bonus": str(bonus), "quota": quota, "note": note})))
    await feed.add(db, "topup", f"买家 {feed.mask(buyer['nickname'])} 充值了 {feed.amount_band(amount)}", band=feed.amount_band(amount))
    return await db.fetch_one("SELECT * FROM topup_orders WHERE id = %s", (topup_id,))


async def reconcile(db: Database, gw: NewApi, user: dict) -> dict:
    """对账：平台余额 vs 引擎额度；返回两边的数和差额（不自动改，后台看过再说）。"""
    w = await get_wallet(db, str(user["id"]))
    out = {"balance_cny": w["balance"], "newapi_quota": None, "newapi_cny": None, "diff_cny": None}
    if gw.configured and user.get("newapi_user_id"):
        try:
            u = await gw.get_user(int(user["newapi_user_id"]))
            q = int(u.get("quota") or 0)
            cny = (await quota_to_cny(db, q)).quantize(Q2)
            out.update(newapi_quota=q, newapi_cny=cny, diff_cny=(Decimal(str(w["balance"])) - cny).quantize(Q2))
        except NewApiError as e:
            out["error"] = e.message
    return out
