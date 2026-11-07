"""调用记录同步：从转发引擎的消费日志拉到 usage_logs，顺带扣买方余额、算卖方收益。

游标：sync_state['usage_cursor'] = 最后处理过的引擎日志 id。引擎按 id 倒序分页返回，所以每轮从第 1 页往后翻，
翻到 id <= 游标为止（翻页期间来了新日志会把旧的挤到下一页，所以同一轮内按 id 去重）。
金额：
* 买方付的钱 buyer_cost_cny = quota ÷ 每美元额度 × 汇率（quota 已含售价折扣，是引擎真正扣掉的）
* 官方价 official_cost_usd：token 模型按 token 数 × 目录里的官方单价；其他模式按买方付的钱 ÷ 售价折扣倒推
* 卖方收益 seller_earning_cny = official × 汇率 × 该挂单的结算折扣；差额是平台毛利
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from ..db import Database, jsonb
from ..newapi import NewApi, NewApiError
from . import configs, wallet
from .catalog import sell_discount

Q6 = Decimal("0.000001")


async def _cursor(db: Database) -> int:
    row = await db.fetch_one("SELECT value FROM sync_state WHERE key = 'usage_cursor'")
    return int(row["value"]) if row else 0


async def _set_cursor(db: Database, v: int) -> None:
    await db.execute("INSERT INTO sync_state (key, value, updated_at) VALUES ('usage_cursor', %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (jsonb(v),))


async def sync(db: Database, gw: NewApi, *, max_pages: int | None = None) -> dict:
    if not gw.configured:
        return {"synced": 0, "skipped": "gateway_unconfigured"}
    cursor = await _cursor(db)
    pages = max_pages or int(await configs.num(db, "max_sync_pages"))
    fresh: list[dict] = []
    seen: set[int] = set()
    truncated = False
    for p in range(1, pages + 1):
        page = await gw.list_logs(p=p, page_size=100, log_type=2)
        items = page["items"]
        stop = False
        for it in items:
            lid = int(it.get("id") or 0)
            if lid <= cursor:
                stop = True
                break
            if lid in seen:
                continue
            seen.add(lid)
            fresh.append(it)
        if stop or len(items) < 100:
            break
        if p == pages:
            truncated = True
    if not fresh:
        return {"synced": 0, "cursor": cursor, "truncated": truncated}
    fresh.sort(key=lambda x: int(x["id"]))
    fx = await configs.dec(db, "fx_usd_cny")
    qpu = Decimal(str(await configs.get(db, "newapi_quota_per_unit")))
    buyers = {int(r["newapi_user_id"]): r for r in await db.fetch_all("SELECT id, newapi_user_id, nickname FROM users WHERE newapi_user_id IS NOT NULL")}
    tokens = {int(r["newapi_token_id"]): r for r in await db.fetch_all("SELECT id, buyer_id, newapi_token_id FROM api_tokens WHERE newapi_token_id IS NOT NULL")}
    keys = {int(r["channel_id"]): r for r in await db.fetch_all("SELECT id, seller_id, channel_id FROM provider_keys WHERE channel_id IS NOT NULL")}
    models = {r["id"]: r for r in await db.fetch_all("SELECT * FROM models")}
    listings = {(str(r["key_id"]), r["model_id"]): r for r in await db.fetch_all("SELECT id, key_id, model_id, settle_discount FROM listings WHERE status <> 'removed'")}
    discounts: dict[str, Decimal] = {}
    written, unmatched = 0, 0
    for it in fresh:
        uid = int(it.get("user_id") or 0)
        buyer = buyers.get(uid)
        if not buyer:
            unmatched += 1
            continue
        tok = tokens.get(int(it.get("token_id") or 0))
        model_id = str(it.get("model_name") or "")
        model = models.get(model_id)
        key = keys.get(int(it.get("channel") or 0))
        listing = listings.get((str(key["id"]), model_id)) if key else None
        quota = Decimal(int(it.get("quota") or 0))
        buyer_cny = (quota / qpu * fx).quantize(Q6)
        pt, ct = int(it.get("prompt_tokens") or 0), int(it.get("completion_tokens") or 0)
        if model and model["billing_mode"] == "token" and model["official_input_usd"] is not None:
            official = (Decimal(pt) * Decimal(str(model["official_input_usd"])) + Decimal(ct) * Decimal(str(model["official_output_usd"] or 0))) / Decimal(1_000_000)
        else:
            if model_id not in discounts:
                discounts[model_id] = await sell_discount(db, model_id)
            d = discounts[model_id]
            official = (buyer_cny / fx / d) if d > 0 else Decimal(0)
        official = official.quantize(Q6)
        seller_cny = (official * fx * Decimal(str(listing["settle_discount"]))).quantize(Q6) if listing else Decimal(0)
        margin = (buyer_cny - seller_cny).quantize(Q6)
        created = datetime.fromtimestamp(int(it.get("created_at") or 0), tz=timezone.utc)
        units = None
        if model and model["billing_mode"] == "video_second":
            units = Decimal(5)     # 引擎按次记账；秒数按默认时长估，等接了视频任务回调再精确
        elif model and model["billing_mode"] in ("image", "request"):
            units = Decimal(1)
        async with db.connection() as conn:
            await conn.execute(
                "INSERT INTO usage_logs (request_id, created_at, buyer_id, api_token_id, model_id, listing_id, key_id, seller_id, input_tokens, output_tokens, units, "
                "official_cost_usd, fx_rate, buyer_cost_cny, seller_earning_cny, platform_margin_cny, status, http_status, latency_ms, newapi_log_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ok', 200, %s, %s)",
                (uuid.uuid4(), created, buyer["id"], tok["id"] if tok else uuid.UUID(int=0), model_id, listing["id"] if listing else None,
                 key["id"] if key else None, key["seller_id"] if key else None, pt, ct, units, official, fx, buyer_cny, seller_cny, margin,
                 int(float(it.get("use_time") or 0) * 1000), int(it["id"])))
            if buyer_cny > 0:
                await wallet.post_transaction(conn, str(buyer["id"]), "consume", -buyer_cny, ref_type="usage_log", ref_id=str(it["id"]),
                                              note=f"{model_id} {pt}+{ct} tokens" if pt or ct else model_id, allow_negative=True)
            if tok:
                await conn.execute("UPDATE api_tokens SET quota_used = quota_used + %s, last_used_at = %s WHERE id = %s", (buyer_cny, created, tok["id"]))
            if key:
                await conn.execute("UPDATE provider_keys SET last_used_at = %s, used_today_usd = used_today_usd + %s WHERE id = %s", (created, official, key["id"]))
            bucket = created.replace(minute=0, second=0, microsecond=0)
            await conn.execute(
                "INSERT INTO usage_hourly (bucket, model_id, requests, ok_requests, official_cost_usd, buyer_cost_cny, avg_latency_ms) VALUES (%s, %s, 1, 1, %s, %s, %s) "
                "ON CONFLICT (bucket, model_id) DO UPDATE SET requests = usage_hourly.requests + 1, ok_requests = usage_hourly.ok_requests + 1, "
                "official_cost_usd = usage_hourly.official_cost_usd + EXCLUDED.official_cost_usd, buyer_cost_cny = usage_hourly.buyer_cost_cny + EXCLUDED.buyer_cost_cny",
                (bucket, model_id, official, buyer_cny, int(float(it.get("use_time") or 0) * 1000)))
        written += 1
    new_cursor = max(int(x["id"]) for x in fresh)
    await _set_cursor(db, new_cursor)
    return {"synced": written, "unmatched": unmatched, "cursor": new_cursor, "truncated": truncated}


async def reset_daily_caps(db: Database) -> None:
    await db.execute("UPDATE provider_keys SET used_today_usd = 0")


# ------------------------------------------------------------------ 查询
async def buyer_usage(db: Database, buyer_id: str, days: int = 30, limit: int = 100, offset: int = 0) -> list[dict]:
    return await db.fetch_all(
        "SELECT id, request_id, created_at, model_id, api_token_id, input_tokens, output_tokens, units, buyer_cost_cny, status, latency_ms, is_demo "
        "FROM usage_logs WHERE buyer_id = %s AND created_at >= now() - make_interval(days => %s) ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (buyer_id, max(1, min(365, days)), max(1, min(500, limit)), max(0, offset)))


async def buyer_summary(db: Database, buyer_id: str, days: int = 30) -> dict:
    rows = await db.fetch_all(
        "SELECT model_id, COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(buyer_cost_cny), 0) AS cost_cny FROM usage_logs WHERE buyer_id = %s AND created_at >= now() - make_interval(days => %s) GROUP BY model_id ORDER BY cost_cny DESC",
        (buyer_id, max(1, min(365, days))))
    daily = await db.fetch_all(
        "SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS requests, COALESCE(SUM(buyer_cost_cny), 0) AS cost_cny "
        "FROM usage_logs WHERE buyer_id = %s AND created_at >= now() - make_interval(days => %s) GROUP BY day ORDER BY day",
        (buyer_id, max(1, min(365, days))))
    total = sum((Decimal(str(r["cost_cny"])) for r in rows), Decimal(0))
    return {"days": days, "total_cost_cny": total, "total_requests": sum(int(r["requests"]) for r in rows), "by_model": rows, "daily": daily}


async def seller_usage(db: Database, seller_id: str, days: int = 30) -> dict:
    by_model = await db.fetch_all(
        "SELECT model_id, COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(official_cost_usd), 0) AS official_usd, COALESCE(SUM(seller_earning_cny), 0) AS earning_cny "
        "FROM usage_logs WHERE seller_id = %s AND status = 'ok' AND created_at >= now() - make_interval(days => %s) GROUP BY model_id ORDER BY earning_cny DESC",
        (seller_id, max(1, min(365, days))))
    daily = await db.fetch_all(
        "SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS requests, COALESCE(SUM(seller_earning_cny), 0) AS earning_cny "
        "FROM usage_logs WHERE seller_id = %s AND status = 'ok' AND created_at >= now() - make_interval(days => %s) GROUP BY day ORDER BY day",
        (seller_id, max(1, min(365, days))))
    return {"days": days, "by_model": by_model, "daily": daily,
            "total_earning_cny": sum((Decimal(str(r["earning_cny"])) for r in by_model), Decimal(0)), "total_requests": sum(int(r["requests"]) for r in by_model)}


async def seller_earnings(db: Database, seller_id: str) -> dict:
    """卖方首页的几个数：今日/本月/累计收益、待结算、可提现、保证金。"""
    row = await db.fetch_one(
        "SELECT COALESCE(SUM(seller_earning_cny) FILTER (WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'), 0) AS today, "
        "COALESCE(SUM(seller_earning_cny) FILTER (WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'), 0) AS month, "
        "COALESCE(SUM(seller_earning_cny), 0) AS total, COUNT(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS requests_24h, "
        "COALESCE(SUM(seller_earning_cny) FILTER (WHERE settlement_id IS NULL), 0) AS unsettled "
        "FROM usage_logs WHERE seller_id = %s AND status = 'ok'", (seller_id,))
    w = await wallet.get_wallet(db, seller_id)
    sp = await db.fetch_one("SELECT deposit_required, deposit_balance, settlement_cycle_days FROM seller_profiles WHERE user_id = %s", (seller_id,))
    return {"today_cny": row["today"], "month_cny": row["month"], "total_cny": row["total"], "requests_24h": row["requests_24h"], "unsettled_cny": row["unsettled"],
            "pending_payout_cny": w["pending_payout"], "deposit_balance": sp["deposit_balance"] if sp else 0, "deposit_required": sp["deposit_required"] if sp else 0,
            "settlement_cycle_days": sp["settlement_cycle_days"] if sp else None}
