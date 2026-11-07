"""结算、提现、信用。

结算（定时任务，每天跑一次就够）：对每个卖方，把 settlement_id 为空、日期在「上一个已结束周期」里的调用汇总成一张结算单；
payable = 收益 − 扣款 − 手续费预提；到期（period_end + 周期天数）自动变成「可提现」并转进 wallets.pending_payout。
提现：卖方从 pending_payout 申请，后台审核打款。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..db import Database, jsonb
from ..errors import ApiError, bad, not_found
from . import configs, feed, sellers

Q2 = Decimal("0.01")
SH = timezone(timedelta(hours=8))


def _today() -> date:
    return datetime.now(SH).date()


async def run(db: Database, today: date | None = None) -> dict:
    """生成结算单 + 把到期的结算单变成可提现。"""
    today = today or _today()
    created, matured = 0, 0
    sellers_rows = await db.fetch_all("SELECT u.id, p.settlement_cycle_days FROM users u JOIN seller_profiles p ON p.user_id = u.id WHERE u.role IN ('seller', 'both') AND p.reviewed_at IS NOT NULL")
    fee_rate = await configs.dec(db, "payout_fee_rate")
    for s in sellers_rows:
        cycle = max(1, int(s["settlement_cycle_days"] or 7))
        # 未结算记录里最早的那天开始，按周期切，直到上一个完整周期
        first = await db.fetch_val("SELECT MIN((created_at AT TIME ZONE 'Asia/Shanghai')::date) FROM usage_logs WHERE seller_id = %s AND status = 'ok' AND settlement_id IS NULL", (s["id"],))
        if not first:
            continue
        start = first
        while start + timedelta(days=cycle) <= today:
            end = start + timedelta(days=cycle - 1)
            async with db.connection() as conn:
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(seller_earning_cny), 0) AS gross, COUNT(*) AS n FROM usage_logs WHERE seller_id = %s AND status = 'ok' AND settlement_id IS NULL "
                    "AND (created_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s", (s["id"], start, end))
                agg = await cur.fetchone()
                if int(agg["n"]) > 0:
                    gross = Decimal(str(agg["gross"])).quantize(Q2)
                    cur = await conn.execute("SELECT COALESCE(SUM(amount), 0) AS d FROM deductions WHERE seller_id = %s AND settlement_id IS NULL", (s["id"],))
                    deduction = Decimal(str((await cur.fetchone())["d"])).quantize(Q2)
                    fee = (gross * fee_rate).quantize(Q2)
                    payable = max(Decimal(0), gross - deduction - fee)
                    payable_at = datetime.combine(end + timedelta(days=cycle), datetime.min.time(), tzinfo=SH)
                    cur = await conn.execute(
                        "INSERT INTO settlements (seller_id, period_start, period_end, gross_earning, deduction, fee, payable, status, payable_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'closed', %s) ON CONFLICT (seller_id, period_start) DO NOTHING RETURNING id",
                        (s["id"], start, end, gross, deduction, fee, payable, payable_at))
                    row = await cur.fetchone()
                    if row:
                        await conn.execute("UPDATE usage_logs SET settlement_id = %s WHERE seller_id = %s AND status = 'ok' AND settlement_id IS NULL AND (created_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s",
                                           (row["id"], s["id"], start, end))
                        await conn.execute("UPDATE deductions SET settlement_id = %s WHERE seller_id = %s AND settlement_id IS NULL", (row["id"], s["id"]))
                        created += 1
            start = end + timedelta(days=1)
    # 到期 → 可提现
    due = await db.fetch_all("SELECT * FROM settlements WHERE status = 'closed' AND payable_at <= now()")
    for st in due:
        async with db.connection() as conn:
            await conn.execute("UPDATE wallets SET pending_payout = pending_payout + %s, version = version + 1, updated_at = now() WHERE user_id = %s", (st["payable"], st["seller_id"]))
            await conn.execute("UPDATE settlements SET status = 'payable' WHERE id = %s", (st["id"],))
        matured += 1
        name = await db.fetch_val("SELECT display_name FROM seller_profiles WHERE user_id = %s", (st["seller_id"],))
        await feed.add(db, "settlement_paid", f"供应商 {feed.mask(name or '')} 完成一期结算（{feed.amount_band(Decimal(str(st['payable'])))}）", band=feed.amount_band(Decimal(str(st["payable"]))))
    return {"created": created, "matured": matured}


async def list_settlements(db: Database, seller_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT s.*, p.display_name FROM settlements s LEFT JOIN seller_profiles p ON p.user_id = s.seller_id WHERE TRUE"
    params: list = []
    if seller_id:
        sql += " AND s.seller_id = %s"
        params.append(seller_id)
    if status:
        sql += " AND s.status = %s"
        params.append(status)
    sql += " ORDER BY s.period_start DESC LIMIT %s"
    params.append(max(1, min(200, limit)))
    return await db.fetch_all(sql, params)


async def settlement_detail(db: Database, settlement_id: str, seller_id: str | None = None) -> dict:
    row = await db.fetch_one("SELECT * FROM settlements WHERE id = %s", (settlement_id,))
    if not row or (seller_id and str(row["seller_id"]) != str(seller_id)):
        raise not_found("结算单")
    by_model = await db.fetch_all("SELECT model_id, COUNT(*) AS requests, COALESCE(SUM(seller_earning_cny), 0) AS earning_cny FROM usage_logs WHERE settlement_id = %s GROUP BY model_id ORDER BY earning_cny DESC", (settlement_id,))
    deductions = await db.fetch_all("SELECT id, reason, amount, note, created_at FROM deductions WHERE settlement_id = %s ORDER BY created_at", (settlement_id,))
    return {**row, "by_model": by_model, "deductions": deductions}


async def hold(db: Database, admin_id: str, settlement_id: str, *, held: bool, reason: str | None) -> dict:
    row = await db.fetch_one("SELECT * FROM settlements WHERE id = %s", (settlement_id,))
    if not row:
        raise not_found("结算单")
    if held and row["status"] not in ("closed", "payable"):
        raise ApiError(409, "bad_status", "这张结算单现在不能冻结")
    if not held and row["status"] != "held":
        raise ApiError(409, "bad_status", "这张结算单没有被冻结")
    new_status = "held" if held else "closed"
    async with db.connection() as conn:
        if held and row["status"] == "payable":
            await conn.execute("UPDATE wallets SET pending_payout = pending_payout - %s, version = version + 1 WHERE user_id = %s", (row["payable"], row["seller_id"]))
        await conn.execute("UPDATE settlements SET status = %s, hold_reason = %s WHERE id = %s", (new_status, reason if held else None, settlement_id))
        await conn.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, %s, 'settlement', %s, %s)",
                           (admin_id, "settlement.hold" if held else "settlement.release", settlement_id, jsonb({"reason": reason})))
    return await db.fetch_one("SELECT * FROM settlements WHERE id = %s", (settlement_id,))


async def add_deduction(db: Database, admin_id: str, *, seller_id: str, reason: str, amount: Decimal, note: str | None, ref_type: str | None = None, ref_id: str | None = None) -> dict:
    if reason not in ("key_dead", "dispute", "upstream_rollback", "manual"):
        raise bad("reason 只能是 key_dead / dispute / upstream_rollback / manual", "reason")
    if amount <= 0:
        raise bad("扣款金额要大于 0", "amount")
    row = await db.fetch_one("INSERT INTO deductions (seller_id, reason, amount, ref_type, ref_id, note, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                             (seller_id, reason, amount.quantize(Q2), ref_type, ref_id, note, admin_id))
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'deduction.add', 'seller', %s, %s)",
                     (admin_id, seller_id, jsonb({"reason": reason, "amount": str(amount), "note": note})))
    return row


# ------------------------------------------------------------------ 提现
async def list_payouts(db: Database, seller_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT r.id, r.seller_id, r.amount, r.fee, r.status, r.paid_at, r.proof_url, r.created_at, r.is_demo, p.display_name, r.payout_method->>'type' AS method_type FROM payout_requests r LEFT JOIN seller_profiles p ON p.user_id = r.seller_id WHERE TRUE"
    params: list = []
    if seller_id:
        sql += " AND r.seller_id = %s"
        params.append(seller_id)
    if status:
        sql += " AND r.status = %s"
        params.append(status)
    sql += " ORDER BY r.created_at DESC LIMIT %s"
    params.append(max(1, min(200, limit)))
    return await db.fetch_all(sql, params)


async def request_payout(db: Database, seller_id: str, amount: Decimal) -> dict:
    min_cny = await configs.dec(db, "min_payout_cny")
    if amount < min_cny:
        raise bad(f"最低提现 {min_cny} 元", "amount")
    pm = await db.fetch_val("SELECT payout_method FROM seller_profiles WHERE user_id = %s", (seller_id,))
    if not pm:
        raise ApiError(400, "no_payout_method", "请先在资料里填写收款方式")
    fee = (amount * await configs.dec(db, "payout_fee_rate")).quantize(Q2)
    async with db.connection() as conn:
        cur = await conn.execute("SELECT pending_payout FROM wallets WHERE user_id = %s FOR UPDATE", (seller_id,))
        w = await cur.fetchone()
        if not w or Decimal(str(w["pending_payout"])) < amount:
            raise ApiError(400, "insufficient_payout", f"可提现金额只有 {w['pending_payout'] if w else 0} 元")
        await conn.execute("UPDATE wallets SET pending_payout = pending_payout - %s, version = version + 1, updated_at = now() WHERE user_id = %s", (amount, seller_id))
        cur = await conn.execute("INSERT INTO payout_requests (seller_id, amount, fee, payout_method) VALUES (%s, %s, %s, %s) RETURNING *", (seller_id, amount.quantize(Q2), fee, jsonb(pm)))
        row = await cur.fetchone()
    return {k: v for k, v in row.items() if k != "payout_method"}


async def review_payout(db: Database, admin_id: str, payout_id: str, *, approve: bool, proof_url: str | None = None, note: str | None = None) -> dict:
    row = await db.fetch_one("SELECT * FROM payout_requests WHERE id = %s", (payout_id,))
    if not row:
        raise not_found("提现申请")
    if row["status"] not in ("pending", "approved"):
        raise ApiError(409, "already_reviewed", "这笔提现已经处理过了")
    amount = Decimal(str(row["amount"]))
    async with db.connection() as conn:
        if approve:
            # 打款完成：记流水（出账），状态 paid
            cur = await conn.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (row["seller_id"],))
            w = await cur.fetchone()
            await conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, balance_after, ref_type, ref_id, note) VALUES (%s, 'payout', %s, %s, 'payout', %s, %s)",
                               (row["seller_id"], -amount, w["balance"] if w else 0, str(payout_id), note))
            await conn.execute("UPDATE payout_requests SET status = 'paid', reviewed_by = %s, paid_at = now(), proof_url = %s WHERE id = %s", (admin_id, proof_url, payout_id))
        else:
            await conn.execute("UPDATE wallets SET pending_payout = pending_payout + %s, version = version + 1 WHERE user_id = %s", (amount, row["seller_id"]))
            await conn.execute("UPDATE payout_requests SET status = 'rejected', reviewed_by = %s WHERE id = %s", (admin_id, payout_id))
        await conn.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, %s, 'payout', %s, %s)",
                           (admin_id, "payout.paid" if approve else "payout.reject", payout_id, jsonb({"amount": str(amount), "note": note})))
    if approve:
        await _credit(db, str(row["seller_id"]), "payout_ontime", 1, "按时完成一笔提现")
    out = await db.fetch_one("SELECT * FROM payout_requests WHERE id = %s", (payout_id,))
    return {k: v for k, v in out.items() if k != "payout_method"}


async def payout_detail_for_admin(db: Database, payout_id: str) -> dict:
    row = await db.fetch_one("SELECT r.*, p.display_name, u.nickname FROM payout_requests r JOIN users u ON u.id = r.seller_id LEFT JOIN seller_profiles p ON p.user_id = r.seller_id WHERE r.id = %s", (payout_id,))
    if not row:
        raise not_found("提现申请")
    out = dict(row)
    out["payout_method"] = await sellers.payout_method_plain(db, str(row["seller_id"]))
    return out


async def _credit(db: Database, user_id: str, event: str, delta: int, note: str) -> None:
    async with db.connection() as conn:
        cur = await conn.execute("UPDATE users SET credit_score = GREATEST(0, LEAST(100, credit_score + %s)), updated_at = now() WHERE id = %s RETURNING credit_score", (delta, user_id))
        r = await cur.fetchone()
        if r:
            await conn.execute("INSERT INTO credit_events (user_id, event, delta, score_after, note) VALUES (%s, %s, %s, %s, %s)", (user_id, event, delta, r["credit_score"], note))
