"""公开接口的数据：市场页模型列表、首页数字、模型健康度、求购单。所有返回都不含买卖双方的 id。"""
from __future__ import annotations

from decimal import Decimal

from ..db import Database
from . import catalog, configs, feed
from .feed import amount_band


async def market_models(db: Database, include_demo: bool = True) -> list[dict]:
    rows = await db.fetch_all("SELECT * FROM v_market_models" + ("" if include_demo else " WHERE NOT is_demo") + " ORDER BY is_featured DESC, online_sellers DESC, id")
    fx = await configs.dec(db, "fx_usd_cny")
    default = await configs.dec(db, "default_sell_discount")
    out = []
    for r in rows:
        d = Decimal(str(r["sell_discount"])) if r["sell_discount"] is not None else default
        prices = catalog.buyer_prices(r, d, fx)
        out.append({"id": r["id"], "display_name": r["display_name"], "kind": r["kind"], "billing_mode": r["billing_mode"], "provider_id": r["provider_id"],
                    "official_input_usd": r["official_input_usd"], "official_output_usd": r["official_output_usd"], "official_unit_usd": r["official_unit_usd"],
                    "online_sellers": int(r["online_sellers"] or 0), "availability_7d": r["availability_7d"], "is_featured": r["is_featured"], "is_demo": r["is_demo"],
                    "prices": prices})
    return out


async def model_detail(db: Database, model_id: str) -> dict | None:
    rows = [m for m in await market_models(db) if m["id"] == model_id]
    if not rows:
        return None
    m = rows[0]
    hourly = await db.fetch_all("SELECT bucket, requests, ok_requests, avg_latency_ms FROM usage_hourly WHERE model_id = %s AND bucket >= now() - interval '7 days' ORDER BY bucket", (model_id,))
    m["requests_7d"] = sum(int(h["requests"]) for h in hourly)
    m["hourly"] = hourly
    return m


async def model_health(db: Database, model_id: str) -> dict:
    row = await db.fetch_one(
        "SELECT COUNT(DISTINCT k.id) AS keys, COALESCE(AVG(k.success_rate_7d), 0) AS success_rate_7d, COALESCE(AVG(k.avg_latency_ms_7d), 0)::int AS avg_latency_ms "
        "FROM listings l JOIN provider_keys k ON k.id = l.key_id WHERE l.model_id = %s AND l.status = 'active' AND k.status = 'active'", (model_id,))
    recent = await db.fetch_one(
        "SELECT COUNT(*) AS requests, COUNT(*) FILTER (WHERE status = 'ok') AS ok FROM usage_logs WHERE model_id = %s AND created_at >= now() - interval '24 hours'", (model_id,))
    return {"model_id": model_id, "online_keys": int(row["keys"] or 0), "success_rate_7d": row["success_rate_7d"], "avg_latency_ms": row["avg_latency_ms"],
            "requests_24h": int(recent["requests"] or 0), "ok_rate_24h": (Decimal(recent["ok"]) / Decimal(recent["requests"])).quantize(Decimal("0.0001")) if recent["requests"] else None}


async def stats(db: Database) -> dict:
    row = await db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM v_market_models WHERE online_sellers > 0) AS online_models, "
        "(SELECT COUNT(DISTINCT l.seller_id) FROM listings l JOIN provider_keys k ON k.id = l.key_id WHERE l.status = 'active' AND k.status = 'active') AS online_sellers, "
        "(SELECT COUNT(*) FROM users WHERE role IN ('seller', 'both')) AS sellers, (SELECT COUNT(*) FROM users WHERE role IN ('buyer', 'both')) AS buyers, "
        "(SELECT COUNT(*) FROM usage_logs WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai') AS today_requests, "
        "(SELECT COUNT(*) FROM usage_logs WHERE created_at >= now() - interval '7 days') AS requests_7d, "
        "(SELECT COALESCE(SUM(buyer_cost_cny), 0) FROM usage_logs) AS gmv, "
        "(SELECT COALESCE(SUM(buyer_cost_cny), 0) FROM usage_logs WHERE created_at >= now() - interval '7 days') AS gmv_7d, "
        "(SELECT bool_or(is_demo) FROM users) AS includes_demo")
    gmv = Decimal(str(row["gmv"] or 0))
    return {"online_models": int(row["online_models"] or 0), "online_sellers": int(row["online_sellers"] or 0), "sellers": int(row["sellers"] or 0), "buyers": int(row["buyers"] or 0),
            "today_requests": int(row["today_requests"] or 0), "requests_7d": int(row["requests_7d"] or 0), "gmv_band": _gmv_band(gmv), "gmv_7d_band": _gmv_band(Decimal(str(row["gmv_7d"] or 0))),
            "includes_demo": bool(row["includes_demo"])}


def _gmv_band(v: Decimal) -> str:
    for limit, text in ((1000, "1,000 元以内"), (10000, "1,000 – 1 万元"), (100000, "1 万 – 10 万元"), (1000000, "10 万 – 100 万元"), (5000000, "100 万 – 500 万元")):
        if v < limit:
            return text
    return "500 万元以上"


async def demands_with_band(db: Database, limit: int = 30) -> list[dict]:
    rows = await db.fetch_all("SELECT id, model_id, provider_id, description, budget_cny, target_discount, expected_monthly_usd, status, is_demo, created_at FROM demand_requests WHERE status = 'open' ORDER BY created_at DESC LIMIT %s", (max(1, min(100, limit)),))
    out = []
    for r in rows:
        d = dict(r)
        b = d.pop("budget_cny", None)
        d["budget_band"] = amount_band(Decimal(str(b))) if b is not None else None
        out.append(d)
    return out


async def recent_feed(db: Database, limit: int = 30) -> list[dict]:
    return await feed.recent(db, limit)
