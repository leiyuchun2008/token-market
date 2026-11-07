"""后台接口：看板、用户、卖方审核、充值审核、模型与定价、Key 与挂单、结算、提现、扣款、参数、动态、审计、引擎状态、演示数据。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..db import jsonb, plain
from ..deps import AdminUser, Cfg, Db, Gw
from ..errors import ApiError, bad, not_found
from ..newapi import NewApiError
from ..services import catalog, configs, demo, feed, keys, sellers, settlements, usage, wallet

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ReviewIn(BaseModel):
    approve: bool
    note: str | None = None
    deposit_required: Decimal | None = None
    settlement_cycle_days: int | None = Field(default=None, ge=1, le=90)


class ProviderIn(BaseModel):
    id: str
    name: str
    base_url: str | None = None
    api_style: str = "openai"
    channel_type: int | None = None


class ModelIn(BaseModel):
    id: str
    provider_id: str
    display_name: str | None = None
    kind: str
    billing_mode: str
    official_input_usd: Decimal | None = None
    official_output_usd: Decimal | None = None
    official_unit_usd: Decimal | None = None
    upstream_model_name: str | None = None
    is_featured: bool = False
    status: str = "active"


class RuleIn(BaseModel):
    model_id: str | None = None
    buyer_tier: int | None = None
    sell_discount: Decimal
    priority: int = 0
    note: str | None = None


class StatusIn(BaseModel):
    status: str
    reason: str | None = None


class WeightIn(BaseModel):
    weight: int = Field(ge=1, le=1000)


class HoldIn(BaseModel):
    held: bool = True
    reason: str | None = None


class DeductionIn(BaseModel):
    seller_id: str
    reason: str
    amount: Decimal
    note: str | None = None


class PayoutReviewIn(BaseModel):
    approve: bool
    proof_url: str | None = None
    note: str | None = None


class TopupReviewIn(BaseModel):
    approve: bool
    note: str | None = None


class DepositIn(BaseModel):
    amount: Decimal
    note: str | None = None


class UserPatch(BaseModel):
    status: str | None = None
    role: str | None = None
    credit_score: int | None = Field(default=None, ge=0, le=100)


class FeedIn(BaseModel):
    type: str
    display_text: str = Field(min_length=2, max_length=200)
    model_id: str | None = None
    is_demo: bool = True


# ---- 看板 ----
@router.get("/dashboard")
async def dashboard(admin: AdminUser, db: Db, gw: Gw, cfg: Cfg):
    row = await db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM users WHERE role IN ('buyer', 'both')) AS buyers, (SELECT COUNT(*) FROM users WHERE role IN ('seller', 'both')) AS sellers, "
        "(SELECT COUNT(*) FROM seller_profiles WHERE reviewed_at IS NULL) AS pending_sellers, (SELECT COUNT(*) FROM topup_orders WHERE status = 'pending') AS pending_topups, "
        "(SELECT COUNT(*) FROM payout_requests WHERE status = 'pending') AS pending_payouts, (SELECT COUNT(*) FROM provider_keys WHERE status = 'active') AS active_keys, "
        "(SELECT COUNT(*) FROM provider_keys WHERE status = 'dead') AS dead_keys, (SELECT COUNT(*) FROM listings WHERE status = 'active') AS active_listings, "
        "(SELECT COUNT(*) FROM usage_logs WHERE created_at >= now() - interval '24 hours') AS requests_24h, "
        "(SELECT COALESCE(SUM(buyer_cost_cny), 0) FROM usage_logs WHERE created_at >= now() - interval '24 hours') AS gmv_24h, "
        "(SELECT COALESCE(SUM(platform_margin_cny), 0) FROM usage_logs WHERE created_at >= now() - interval '24 hours') AS margin_24h, "
        "(SELECT COALESCE(SUM(buyer_cost_cny), 0) FROM usage_logs WHERE created_at >= date_trunc('month', now())) AS gmv_month, "
        "(SELECT COALESCE(SUM(platform_margin_cny), 0) FROM usage_logs WHERE created_at >= date_trunc('month', now())) AS margin_month, "
        "(SELECT COALESCE(SUM(balance), 0) FROM wallets) AS buyer_balances, (SELECT COALESCE(SUM(pending_payout), 0) FROM wallets) AS pending_payout_total")
    daily = await db.fetch_all(
        "SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS requests, COALESCE(SUM(buyer_cost_cny), 0) AS gmv, COALESCE(SUM(platform_margin_cny), 0) AS margin "
        "FROM usage_logs WHERE created_at >= now() - interval '30 days' GROUP BY day ORDER BY day")
    sync_row = await db.fetch_one("SELECT value, updated_at FROM sync_state WHERE key = 'usage_cursor'")
    gateway = {"configured": gw.configured, "base_url": cfg.newapi_base_url if gw.configured else None}
    if gw.configured:
        try:
            gateway.update(await gw.status(), ok=True)
        except NewApiError as e:
            gateway.update(ok=False, error=e.message)
    return plain({**row, "daily": daily, "gateway": gateway, "usage_cursor": sync_row["value"] if sync_row else 0, "usage_synced_at": sync_row["updated_at"] if sync_row else None})


# ---- 用户 ----
@router.get("/users")
async def list_users(admin: AdminUser, db: Db, role: str | None = None, q: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    sql = "SELECT u.*, w.balance, w.pending_payout, p.display_name AS seller_display_name, p.reviewed_at AS seller_reviewed_at FROM users u LEFT JOIN wallets w ON w.user_id = u.id LEFT JOIN seller_profiles p ON p.user_id = u.id WHERE TRUE"
    params: list = []
    if role:
        sql += " AND u.role = %s"
        params.append(role)
    if q:
        sql += " AND (u.nickname ILIKE %s OR u.email ILIKE %s OR u.phone ILIKE %s)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY u.created_at DESC LIMIT %s OFFSET %s"
    params += [limit, offset]
    rows = await db.fetch_all(sql, params)
    return plain([{k: v for k, v in r.items() if k not in ("password_hash", "newapi_password_ct")} for r in rows])


@router.get("/users/{user_id}")
async def get_user(user_id: str, admin: AdminUser, db: Db, gw: Gw):
    row = await db.fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))
    if not row:
        raise not_found("用户")
    out = {k: v for k, v in row.items() if k not in ("password_hash", "newapi_password_ct")}
    out["wallet"] = await wallet.get_wallet(db, user_id)
    out["seller_profile"] = await sellers.profile(db, user_id)
    out["reconcile"] = await wallet.reconcile(db, gw, row)
    out["credit_events"] = await db.fetch_all("SELECT event, delta, score_after, note, created_at FROM credit_events WHERE user_id = %s ORDER BY created_at DESC LIMIT 20", (user_id,))
    return plain(out)


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: UserPatch, admin: AdminUser, db: Db):
    row = await db.fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))
    if not row:
        raise not_found("用户")
    if body.status is not None:
        if body.status not in ("active", "suspended", "banned"):
            raise bad("状态只能是 active / suspended / banned", "status")
        await db.execute("UPDATE users SET status = %s, updated_at = now() WHERE id = %s", (body.status, user_id))
    if body.role is not None:
        if body.role not in ("buyer", "seller", "both", "admin"):
            raise bad("角色不对", "role")
        await db.execute("UPDATE users SET role = %s, updated_at = now() WHERE id = %s", (body.role, user_id))
    if body.credit_score is not None:
        await db.execute("UPDATE users SET credit_score = %s, updated_at = now() WHERE id = %s", (body.credit_score, user_id))
        await db.execute("INSERT INTO credit_events (user_id, event, delta, score_after, note) VALUES (%s, 'manual', %s, %s, %s)",
                         (user_id, body.credit_score - int(row["credit_score"]), body.credit_score, "后台手工调整"))
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, before, after) VALUES (%s, 'user.update', 'user', %s, %s, %s)",
                     (admin.id, user_id, jsonb({"status": row["status"], "role": row["role"], "credit_score": row["credit_score"]}), jsonb(body.model_dump(exclude_none=True))))
    return plain({k: v for k, v in (await db.fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))).items() if k not in ("password_hash", "newapi_password_ct")})


# ---- 卖方审核 ----
@router.get("/sellers/pending")
async def pending_sellers(admin: AdminUser, db: Db):
    return plain(await sellers.pending(db))


@router.post("/sellers/{user_id}/review")
async def review_seller(user_id: str, body: ReviewIn, admin: AdminUser, db: Db):
    return plain(await sellers.review(db, admin.id, user_id, approve=body.approve, deposit_required=body.deposit_required, settlement_cycle_days=body.settlement_cycle_days, note=body.note))


@router.post("/sellers/{user_id}/deposit")
async def seller_deposit(user_id: str, body: DepositIn, admin: AdminUser, db: Db):
    return plain(await sellers.record_deposit(db, admin.id, user_id, body.amount, body.note))


# ---- 充值审核 ----
@router.get("/topups")
async def list_topups(admin: AdminUser, db: Db, status: str | None = Query("pending")):
    return plain(await wallet.list_topups(db, status=status or None, limit=200))


@router.post("/topups/{topup_id}/review")
async def review_topup(topup_id: str, body: TopupReviewIn, admin: AdminUser, db: Db, gw: Gw):
    return plain(await wallet.review_topup(db, gw, admin.id, topup_id, approve=body.approve, note=body.note))


# ---- 模型目录与定价 ----
@router.get("/providers")
async def list_providers(admin: AdminUser, db: Db):
    return plain(await catalog.list_providers(db))


@router.post("/providers")
async def upsert_provider(body: ProviderIn, admin: AdminUser, db: Db):
    return plain(await catalog.upsert_provider(db, id=body.id, name=body.name, base_url=body.base_url, api_style=body.api_style, channel_type=body.channel_type))


@router.get("/models")
async def list_models(admin: AdminUser, db: Db):
    return plain(await catalog.list_models(db, status=None))


@router.post("/models")
async def upsert_model(body: ModelIn, admin: AdminUser, db: Db):
    return plain(await catalog.upsert_model(db, body.model_dump()))


@router.patch("/models/{model_id}")
async def patch_model(model_id: str, body: dict, admin: AdminUser, db: Db):
    cur = await catalog.get_model(db, model_id)
    merged = {**cur, **{k: v for k, v in body.items() if k in ModelIn.model_fields}}
    merged["id"] = model_id
    return plain(await catalog.upsert_model(db, merged))


@router.get("/pricing-rules")
async def list_rules(admin: AdminUser, db: Db):
    return plain(await catalog.list_rules(db))


@router.post("/pricing-rules", status_code=201)
async def add_rule(body: RuleIn, admin: AdminUser, db: Db):
    return plain(await catalog.add_rule(db, model_id=body.model_id, sell_discount=body.sell_discount, buyer_tier=body.buyer_tier, priority=body.priority, note=body.note))


@router.delete("/pricing-rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str, admin: AdminUser, db: Db):
    await catalog.delete_rule(db, rule_id)
    return None


@router.get("/pricing/preview")
async def pricing_preview(admin: AdminUser, db: Db):
    return plain(await catalog.newapi_pricing(db))


@router.post("/pricing/sync")
async def pricing_sync(admin: AdminUser, db: Db, gw: Gw):
    out = await catalog.sync_pricing(db, gw)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type) VALUES (%s, 'pricing.sync', 'newapi')", (admin.id,))
    return plain(out)


# ---- Key 与挂单 ----
@router.get("/keys")
async def list_all_keys(admin: AdminUser, db: Db, status: str | None = None, limit: int = Query(100, ge=1, le=500)):
    sql = "SELECT k.id, k.seller_id, k.provider_id, k.label, k.status, k.channel_id, k.channel_name, k.consecutive_failures, k.success_rate_7d, k.avg_latency_ms_7d, k.last_probe_at, k.last_used_at, k.daily_cap_usd, k.used_today_usd, k.created_at, k.is_demo, p.display_name AS seller_name, " \
          "(SELECT COUNT(*) FROM listings l WHERE l.key_id = k.id AND l.status = 'active') AS active_listings FROM provider_keys k LEFT JOIN seller_profiles p ON p.user_id = k.seller_id WHERE k.status <> 'removed'"
    params: list = []
    if status:
        sql += " AND k.status = %s"
        params.append(status)
    sql += " ORDER BY k.created_at DESC LIMIT %s"
    params.append(limit)
    return plain(await db.fetch_all(sql, params))


@router.get("/keys/{key_id}")
async def admin_key_detail(key_id: str, admin: AdminUser, db: Db):
    return plain(await keys.key_detail(db, key_id))


@router.post("/keys/{key_id}/status")
async def key_status(key_id: str, body: StatusIn, admin: AdminUser, db: Db, gw: Gw):
    return plain(await keys.set_key_status(db, gw, key_id, body.status, actor=admin.id))


@router.post("/keys/{key_id}/revalidate")
async def admin_revalidate(key_id: str, admin: AdminUser, db: Db, gw: Gw):
    return plain(await keys.revalidate(db, gw, key_id))


@router.get("/listings")
async def list_all_listings(admin: AdminUser, db: Db, model_id: str | None = None, status: str | None = "active"):
    sql = "SELECT l.*, m.display_name AS model_name, p.display_name AS seller_name, k.status AS key_status FROM listings l JOIN models m ON m.id = l.model_id LEFT JOIN seller_profiles p ON p.user_id = l.seller_id JOIN provider_keys k ON k.id = l.key_id WHERE TRUE"
    params: list = []
    if model_id:
        sql += " AND l.model_id = %s"
        params.append(model_id)
    if status:
        sql += " AND l.status = %s"
        params.append(status)
    sql += " ORDER BY l.model_id, l.settle_discount"
    return plain(await db.fetch_all(sql, params))


@router.patch("/listings/{listing_id}/weight")
async def listing_weight(listing_id: str, body: WeightIn, admin: AdminUser, db: Db, gw: Gw):
    return plain(await keys.update_listing(db, gw, None, listing_id, weight=body.weight, actor=admin.id))


@router.post("/listings/{listing_id}/status")
async def listing_status(listing_id: str, body: StatusIn, admin: AdminUser, db: Db, gw: Gw):
    return plain(await keys.update_listing(db, gw, None, listing_id, status=body.status, actor=admin.id))


# ---- 结算、提现、扣款 ----
@router.get("/settlements")
async def list_settlements(admin: AdminUser, db: Db, status: str | None = None):
    return plain(await settlements.list_settlements(db, status=status, limit=200))


@router.post("/settlements/run")
async def run_settlements(admin: AdminUser, db: Db):
    out = await settlements.run(db)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, after) VALUES (%s, 'settlement.run', 'settlement', %s)", (admin.id, jsonb(out)))
    return plain(out)


@router.get("/settlements/{settlement_id}")
async def settlement_detail(settlement_id: str, admin: AdminUser, db: Db):
    return plain(await settlements.settlement_detail(db, settlement_id))


@router.post("/settlements/{settlement_id}/hold")
async def hold_settlement(settlement_id: str, body: HoldIn, admin: AdminUser, db: Db):
    return plain(await settlements.hold(db, admin.id, settlement_id, held=body.held, reason=body.reason))


@router.post("/deductions", status_code=201)
async def add_deduction(body: DeductionIn, admin: AdminUser, db: Db):
    return plain(await settlements.add_deduction(db, admin.id, seller_id=body.seller_id, reason=body.reason, amount=body.amount, note=body.note))


@router.get("/payouts")
async def list_payouts(admin: AdminUser, db: Db, status: str | None = Query("pending")):
    return plain(await settlements.list_payouts(db, status=status or None, limit=200))


@router.get("/payouts/{payout_id}")
async def payout_detail(payout_id: str, admin: AdminUser, db: Db):
    return plain(await settlements.payout_detail_for_admin(db, payout_id))


@router.post("/payouts/{payout_id}/review")
async def review_payout(payout_id: str, body: PayoutReviewIn, admin: AdminUser, db: Db):
    return plain(await settlements.review_payout(db, admin.id, payout_id, approve=body.approve, proof_url=body.proof_url, note=body.note))


# ---- 参数、动态、审计 ----
@router.get("/configs")
async def get_configs(admin: AdminUser, db: Db):
    return plain({"values": await configs.all_configs(db), "editable": configs.EDITABLE})


@router.patch("/configs")
async def patch_configs(body: dict, admin: AdminUser, db: Db):
    for k, v in body.items():
        try:
            await configs.set_config(db, k, v, admin.id)
        except ValueError as e:
            raise bad(str(e), k)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, after) VALUES (%s, 'configs.update', 'system_configs', %s)", (admin.id, jsonb(body)))
    return plain(await configs.all_configs(db))


@router.get("/feed")
async def admin_feed(admin: AdminUser, db: Db, limit: int = Query(100, ge=1, le=500)):
    return plain(await feed.recent(db, limit))


@router.post("/feed", status_code=201)
async def post_feed(body: FeedIn, admin: AdminUser, db: Db):
    """后台手工发一条动态：默认 is_demo=true（前台带「演示」角标）；真实事件由业务自动写入，不走这里。"""
    if body.type not in ("seller_joined", "buyer_joined", "listing_created", "topup", "usage_milestone", "settlement_paid", "demand_posted"):
        raise bad("type 不对", "type")
    await feed.add(db, body.type, body.display_text, model_id=body.model_id, is_demo=body.is_demo)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, after) VALUES (%s, 'feed.post', 'activity_feed', %s)", (admin.id, jsonb(body.model_dump())))
    return plain({"ok": True})


@router.delete("/feed/{feed_id}", status_code=204)
async def delete_feed(feed_id: int, admin: AdminUser, db: Db):
    await db.execute("DELETE FROM activity_feed WHERE id = %s", (feed_id,))
    return None


@router.get("/audit-logs")
async def audit_logs(admin: AdminUser, db: Db, limit: int = Query(100, ge=1, le=500)):
    return plain(await db.fetch_all("SELECT a.*, u.nickname AS actor FROM audit_logs a LEFT JOIN users u ON u.id = a.actor_id ORDER BY a.id DESC LIMIT %s", (limit,)))


# ---- 引擎与任务 ----
@router.get("/gateway")
async def gateway_status(admin: AdminUser, gw: Gw, cfg: Cfg):
    out = {"configured": gw.configured, "base_url": cfg.newapi_base_url}
    if gw.configured:
        try:
            out.update(await gw.status(), ok=True)
        except NewApiError as e:
            out.update(ok=False, error=e.message)
    return plain(out)


@router.post("/jobs/sync-usage")
async def job_sync(admin: AdminUser, db: Db, gw: Gw):
    return plain(await usage.sync(db, gw))


@router.post("/jobs/probe-keys")
async def job_probe(admin: AdminUser, db: Db, gw: Gw):
    return plain(await keys.probe_all(db, gw))


# ---- 演示数据 ----
@router.get("/demo")
async def demo_counts(admin: AdminUser, cfg: Cfg):
    return plain(await demo.counts(cfg.database_url))


@router.post("/demo/seed")
async def demo_seed(admin: AdminUser, db: Db, cfg: Cfg):
    if cfg.env == "prod":
        raise ApiError(403, "prod", "生产环境不能装演示数据")
    out = await demo.seed(cfg.database_url)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type) VALUES (%s, 'demo.seed', 'demo')", (admin.id,))
    return plain(out)


@router.post("/demo/clear")
async def demo_clear(admin: AdminUser, db: Db, cfg: Cfg):
    out = await demo.clear(cfg.database_url)
    await db.execute("INSERT INTO audit_logs (actor_id, action, target_type) VALUES (%s, 'demo.clear', 'demo')", (admin.id,))
    return plain(out)
