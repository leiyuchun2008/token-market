"""卖方 Key 托管与挂单。

一个 Key = 转发引擎里的一个渠道：
* 上传时先让引擎用这个 Key 去上游列模型（fetch_models），列不出来就拒收；
* 通过后建渠道（模型列表 = 该 Key 的挂单；model_mapping 把平台模型名映射到上游模型名）；
* 挂单的暂停/恢复/删除就是改渠道的模型列表；Key 暂停/失效 = 停用渠道；
* 渠道优先级按结算折扣：卖方要价越低（settle_discount 越小），优先级越高，流量先给它。
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

from ..db import Database, jsonb
from ..errors import ApiError, bad, not_found
from ..newapi import NewApi, NewApiError
from ..security import fingerprint, seal, unseal
from . import configs, feed

KEY_STATUS_ROUTABLE = ("active",)


def _hint(secret: str) -> str:
    s = secret.strip()
    return ("*" * 4 + s[-4:]) if len(s) >= 8 else "****"


def public_key(row: dict, probes: list[dict] | None = None) -> dict:
    out = {k: v for k, v in row.items() if k not in ("key_ciphertext", "extra_auth")}
    out["key_hint"] = row.get("key_hint") or ""
    if probes is not None:
        out["recent_probes"] = probes
    return out


async def _priority(settle_discount: Decimal) -> int:
    return int(round((Decimal("1") - settle_discount) * 100))


async def _seller_short(db: Database, seller_id: str) -> str:
    return uuid.UUID(str(seller_id)).hex[:6]


async def list_keys(db: Database, seller_id: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT k.*, p.name AS provider_name, "
        "(SELECT COUNT(*) FROM listings l WHERE l.key_id = k.id AND l.status = 'active') AS active_listings, "
        "(SELECT COALESCE(SUM(u.seller_earning_cny), 0) FROM usage_logs u WHERE u.key_id = k.id AND u.created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai') AS today_earning_cny "
        "FROM provider_keys k JOIN providers p ON p.id = k.provider_id WHERE k.seller_id = %s AND k.status <> 'removed' ORDER BY k.created_at DESC", (seller_id,))
    return [public_key(r) for r in rows]


async def get_key(db: Database, key_id: str, seller_id: str | None = None) -> dict:
    row = await db.fetch_one("SELECT k.*, p.name AS provider_name FROM provider_keys k JOIN providers p ON p.id = k.provider_id WHERE k.id = %s", (key_id,))
    if not row or (seller_id and str(row["seller_id"]) != str(seller_id)):
        raise not_found("Key")
    return row


async def key_detail(db: Database, key_id: str, seller_id: str | None = None) -> dict:
    row = await get_key(db, key_id, seller_id)
    probes = await db.fetch_all("SELECT probed_at, ok, latency_ms, error_code FROM key_probes WHERE key_id = %s ORDER BY probed_at DESC LIMIT 20", (key_id,))
    listings = await db.fetch_all("SELECT id, model_id, settle_discount, weight, status, created_at FROM listings WHERE key_id = %s AND status <> 'removed' ORDER BY created_at", (key_id,))
    d = public_key(row, probes)
    d["listings"] = listings
    return d


async def add_key(db: Database, gw: NewApi, seller: dict, *, provider_id: str, secret: str, label: str | None = None,
                  base_url_override: str | None = None, daily_cap_usd: Decimal | None = None, extra_auth: dict | None = None) -> dict:
    secret = (secret or "").strip()
    if not 8 <= len(secret) <= 1024 or any(ch.isspace() for ch in secret):
        raise bad("Key 格式不对（8 到 1024 个字符，不含空格）", "secret")
    prov = await db.fetch_one("SELECT * FROM providers WHERE id = %s", (provider_id,))
    if not prov:
        raise bad("厂商不存在", "provider_id")
    base_url = (base_url_override or prov.get("base_url") or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise bad("上游地址要以 http:// 或 https:// 开头", "base_url_override")
    fp = fingerprint(secret)
    dup = await db.fetch_one("SELECT id, status FROM provider_keys WHERE seller_id = %s AND key_fingerprint = %s AND status <> 'removed'", (seller["id"], fp))
    if dup:
        raise ApiError(409, "duplicate_key", "这个 Key 已经托管过了")
    if not gw.configured:
        raise ApiError(503, "gateway_unconfigured", "转发引擎还没接入，暂时不能校验 Key")
    # 1. 让引擎拿这个 Key 去上游列模型：列不出来 = Key 无效 / 地址不对
    try:
        upstream_models = await gw.fetch_models(base_url, secret, int(prov["channel_type"]))
    except NewApiError as e:
        if e.status >= 500 or e.status in (401, 403):
            raise
        raise ApiError(400, "key_invalid", f"Key 校验失败：{e.message}", "secret")
    if not upstream_models:
        raise ApiError(400, "key_invalid", "Key 校验失败：上游没有返回任何模型", "secret")
    # 2. 建渠道（先不挂任何模型 → 停用状态；挂单后再启用）
    n = (await db.fetch_val("SELECT COUNT(*) FROM provider_keys WHERE seller_id = %s", (seller["id"],))) or 0
    short = await _seller_short(db, seller["id"])
    channel_name = f"mk-{short}-{int(n) + 1}"
    group = str(await configs.get(db, "default_channel_group") or "default")
    ch = await gw.create_channel(name=channel_name, key=secret, base_url=base_url, models=[upstream_models[0]], channel_type=int(prov["channel_type"]),
                                 group=group, priority=0, remark=f"token-market seller {short} / {label or ''}".strip(), status=1)
    await gw.set_channel_status(int(ch["id"]), False)
    # 3. 入库（Key 加密保存一份：引擎换机或重建渠道时用）
    row = await db.fetch_one(
        "INSERT INTO provider_keys (seller_id, provider_id, label, key_ciphertext, key_fingerprint, key_hint, base_url_override, extra_auth, status, daily_cap_usd, "
        "channel_id, channel_name, upstream_models, last_probe_at, success_rate_7d) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, now(), 1) RETURNING *",
        (seller["id"], provider_id, (label or _hint(secret))[:80], seal(secret), fp, _hint(secret), base_url_override or None, jsonb(extra_auth) if extra_auth else None,
         daily_cap_usd, int(ch["id"]), channel_name, upstream_models))
    await db.execute("INSERT INTO key_probes (key_id, ok, latency_ms, error_code) VALUES (%s, true, 0, 'validated')", (row["id"],))
    d = public_key(row)
    d["upstream_models"] = upstream_models
    d["suggested_models"] = await _match_models(db, provider_id, upstream_models)
    return d


async def _match_models(db: Database, provider_id: str, upstream_models: list[str]) -> list[dict]:
    """平台目录里这个厂商的模型中，上游名出现在该 Key 可见列表里的（挂单建议）。"""
    rows = await db.fetch_all("SELECT id, display_name, upstream_model_name, kind, billing_mode FROM models WHERE provider_id = %s AND status = 'active'", (provider_id,))
    seen = set(upstream_models)
    return [r for r in rows if r["upstream_model_name"] in seen or r["id"] in seen]


async def _sync_channel(db: Database, gw: NewApi, key_row: dict) -> None:
    """按这个 Key 的有效挂单重写渠道：模型列表、名称映射、优先级、启停。"""
    if not key_row.get("channel_id"):
        return
    ls = await db.fetch_all(
        "SELECT l.model_id, l.settle_discount, m.upstream_model_name FROM listings l JOIN models m ON m.id = l.model_id "
        "WHERE l.key_id = %s AND l.status = 'active' AND m.status = 'active'", (key_row["id"],))
    cid = int(key_row["channel_id"])
    if not ls or key_row["status"] not in KEY_STATUS_ROUTABLE:
        await gw.set_channel_status(cid, False)
        return
    mapping = {l["model_id"]: l["upstream_model_name"] for l in ls if l["upstream_model_name"] and l["upstream_model_name"] != l["model_id"]}
    min_disc = min(Decimal(str(l["settle_discount"])) for l in ls)
    await gw.patch_channel(cid, models=",".join(l["model_id"] for l in ls), model_mapping=json.dumps(mapping, ensure_ascii=False) if mapping else "", priority=await _priority(min_disc))
    await gw.set_channel_status(cid, True)


async def set_key_status(db: Database, gw: NewApi, key_id: str, status: str, *, seller_id: str | None = None, actor: str | None = None) -> dict:
    row = await get_key(db, key_id, seller_id)
    if status not in ("active", "paused", "removed"):
        raise bad("状态只能是 active / paused / removed", "status")
    if row["status"] == "dead" and status == "active":
        raise ApiError(400, "key_dead", "这个 Key 已判定失效，请先点「重新校验」")
    if status == "removed":
        await db.execute("UPDATE listings SET status = 'removed' WHERE key_id = %s", (key_id,))
        if row.get("channel_id"):
            try:
                await gw.delete_channel(int(row["channel_id"]))
            except NewApiError:
                pass
        await db.execute("UPDATE provider_keys SET status = 'removed', channel_id = NULL WHERE id = %s", (key_id,))
    else:
        await db.execute("UPDATE provider_keys SET status = %s, consecutive_failures = CASE WHEN %s = 'active' THEN 0 ELSE consecutive_failures END WHERE id = %s", (status, status, key_id))
        row = await get_key(db, key_id)
        await _sync_channel(db, gw, row)
    if actor:
        await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'key.status', 'provider_key', %s, %s)",
                         (actor, key_id, jsonb({"status": status})))
    return await key_detail(db, key_id)


async def update_key(db: Database, key_id: str, seller_id: str, *, label: str | None = None, daily_cap_usd: Decimal | None = ...) -> dict:
    await get_key(db, key_id, seller_id)
    if label is not None:
        await db.execute("UPDATE provider_keys SET label = %s WHERE id = %s", (label.strip()[:80], key_id))
    if daily_cap_usd is not ...:
        await db.execute("UPDATE provider_keys SET daily_cap_usd = %s WHERE id = %s", (daily_cap_usd, key_id))
    return await key_detail(db, key_id)


async def revalidate(db: Database, gw: NewApi, key_id: str, seller_id: str | None = None) -> dict:
    """重新校验：引擎用渠道里存的 Key 测一次；通过就恢复 active。"""
    row = await get_key(db, key_id, seller_id)
    if row["status"] == "removed":
        raise not_found("Key")
    if not row.get("channel_id"):
        raise ApiError(400, "no_channel", "这个 Key 没有对应的渠道")
    ok, msg, ms = await _probe_once(db, gw, row)
    if ok:
        await db.execute("UPDATE provider_keys SET status = 'active', consecutive_failures = 0, dead_at = NULL WHERE id = %s AND status IN ('dead', 'paused', 'exhausted', 'validating', 'active')", (key_id,))
        await db.execute("UPDATE listings SET status = 'active' WHERE key_id = %s AND status = 'paused'", (key_id,))
        await _sync_channel(db, gw, await get_key(db, key_id))
    d = await key_detail(db, key_id)
    d["revalidation"] = {"ok": ok, "message": msg, "latency_ms": ms}
    return d


async def _probe_once(db: Database, gw: NewApi, row: dict) -> tuple[bool, str, int]:
    model = await db.fetch_val("SELECT model_id FROM listings WHERE key_id = %s AND status = 'active' ORDER BY created_at LIMIT 1", (row["id"],))
    if not model:
        # 没挂单：渠道上只有校验时放的那个上游模型，拿渠道配置里的模型测
        ch = await gw.get_channel(int(row["channel_id"]))
        model = (ch.get("models") or "").split(",")[0].strip() or None
        if not model:
            return True, "没有可测的模型", 0
        # 渠道停用时也能 test
    r = await gw.test_channel(int(row["channel_id"]), model)
    ms = int(round(float(r.get("seconds") or 0) * 1000))
    await db.execute("INSERT INTO key_probes (key_id, ok, latency_ms, error_code) VALUES (%s, %s, %s, %s)",
                     (row["id"], bool(r["ok"]), ms, None if r["ok"] else (r.get("message") or "error")[:60]))
    return bool(r["ok"]), r.get("message") or "", ms


async def probe_all(db: Database, gw: NewApi) -> dict:
    """定时探活：对每个 active 的 Key 测一次；连续失败到阈值 → dead，挂单暂停，渠道停用，卖方扣信用。"""
    if not gw.configured:
        return {"probed": 0, "skipped": "gateway_unconfigured"}
    dead_after = int(await configs.num(db, "key_dead_after_failures"))
    rows = await db.fetch_all("SELECT * FROM provider_keys WHERE status IN ('active', 'dead') AND channel_id IS NOT NULL AND NOT is_demo")
    probed, died, revived = 0, [], []
    for row in rows:
        try:
            ok, _msg, _ms = await _probe_once(db, gw, row)
        except NewApiError:
            break   # 引擎本身连不上：这轮不算失败
        probed += 1
        if ok:
            if row["status"] == "dead":
                await db.execute("UPDATE provider_keys SET status = 'active', consecutive_failures = 0, dead_at = NULL WHERE id = %s", (row["id"],))
                await db.execute("UPDATE listings SET status = 'active' WHERE key_id = %s AND status = 'paused'", (row["id"],))
                await _sync_channel(db, gw, await get_key(db, str(row["id"])))
                revived.append(str(row["id"]))
            else:
                await db.execute("UPDATE provider_keys SET consecutive_failures = 0 WHERE id = %s", (row["id"],))
        else:
            fails = int(row["consecutive_failures"] or 0) + 1
            if row["status"] == "active" and fails >= dead_after:
                await db.execute("UPDATE provider_keys SET status = 'dead', consecutive_failures = %s, dead_at = now() WHERE id = %s", (fails, row["id"]))
                await db.execute("UPDATE listings SET status = 'paused' WHERE key_id = %s AND status = 'active'", (row["id"],))
                await gw.set_channel_status(int(row["channel_id"]), False)
                await _credit(db, str(row["seller_id"]), "key_dead", -10, f"Key {row.get('label') or ''} 连续 {fails} 次探活失败")
                died.append(str(row["id"]))
            else:
                await db.execute("UPDATE provider_keys SET consecutive_failures = %s WHERE id = %s", (fails, row["id"]))
        await _refresh_stats(db, str(row["id"]))
    await db.execute("DELETE FROM key_probes WHERE probed_at < now() - interval '30 days'")
    return {"probed": probed, "died": died, "revived": revived}


async def _refresh_stats(db: Database, key_id: str) -> None:
    await db.execute(
        "UPDATE provider_keys SET last_probe_at = now(), "
        "success_rate_7d = (SELECT AVG(CASE WHEN ok THEN 1 ELSE 0 END) FROM key_probes WHERE key_id = %s AND probed_at >= now() - interval '7 days'), "
        "avg_latency_ms_7d = (SELECT AVG(latency_ms)::int FROM key_probes WHERE key_id = %s AND ok AND probed_at >= now() - interval '7 days') WHERE id = %s",
        (key_id, key_id, key_id))


async def _credit(db: Database, user_id: str, event: str, delta: int, note: str) -> None:
    async with db.connection() as conn:
        cur = await conn.execute("UPDATE users SET credit_score = GREATEST(0, LEAST(100, credit_score + %s)), updated_at = now() WHERE id = %s RETURNING credit_score", (delta, user_id))
        r = await cur.fetchone()
        if r:
            await conn.execute("INSERT INTO credit_events (user_id, event, delta, score_after, note) VALUES (%s, %s, %s, %s, %s)", (user_id, event, delta, r["credit_score"], note))


# ------------------------------------------------------------------ 挂单
async def list_listings(db: Database, seller_id: str) -> list[dict]:
    return await db.fetch_all(
        "SELECT l.*, m.display_name AS model_name, m.kind, m.billing_mode, k.label AS key_label, k.status AS key_status "
        "FROM listings l JOIN models m ON m.id = l.model_id JOIN provider_keys k ON k.id = l.key_id WHERE l.seller_id = %s AND l.status <> 'removed' ORDER BY l.created_at DESC", (seller_id,))


async def add_listing(db: Database, gw: NewApi, seller_id: str, *, key_id: str, model_id: str, settle_discount: Decimal) -> dict:
    key = await get_key(db, key_id, seller_id)
    if key["status"] not in ("active", "paused"):
        raise ApiError(400, "key_unavailable", "这个 Key 现在不能挂单（已失效或已删除）")
    model = await db.fetch_one("SELECT * FROM models WHERE id = %s AND status = 'active'", (model_id,))
    if not model:
        raise bad("模型不存在或已下架", "model_id")
    if model["provider_id"] != key["provider_id"]:
        raise bad("这个模型不属于该 Key 的厂商", "model_id")
    if not Decimal("0.05") <= settle_discount <= Decimal("1"):
        raise bad("结算折扣要在 0.05 到 1 之间（0.6 = 按官方价 6 折结算给你）", "settle_discount")
    sell = await _sell_discount(db, model_id)
    if settle_discount >= sell:
        raise ApiError(400, "discount_too_high", f"结算折扣要低于平台售价折扣 {sell}（否则平台没有差价）", "settle_discount")
    ups = key.get("upstream_models") or []
    if ups and model["upstream_model_name"] not in ups and model_id not in ups:
        raise ApiError(400, "model_not_served", f"校验时这个 Key 看不到上游模型 {model['upstream_model_name']}", "model_id")
    existing = await db.fetch_one("SELECT id, status FROM listings WHERE key_id = %s AND model_id = %s", (key_id, model_id))
    if existing and existing["status"] != "removed":
        raise ApiError(409, "exists", "这个 Key 已经挂了该模型")
    if existing:
        row = await db.fetch_one("UPDATE listings SET status = 'active', settle_discount = %s, created_at = now() WHERE id = %s RETURNING *", (settle_discount, existing["id"]))
    else:
        row = await db.fetch_one("INSERT INTO listings (seller_id, key_id, model_id, settle_discount) VALUES (%s, %s, %s, %s) RETURNING *", (seller_id, key_id, model_id, settle_discount))
    await _sync_channel(db, gw, key)
    name = await db.fetch_val("SELECT display_name FROM seller_profiles WHERE user_id = %s", (seller_id,))
    await feed.add(db, "listing_created", f"供应商 {feed.mask(name or '')} 上架了 {model['display_name']}", model_id=model_id)
    return row


async def _sell_discount(db: Database, model_id: str) -> Decimal:
    from .catalog import sell_discount
    return await sell_discount(db, model_id)


async def update_listing(db: Database, gw: NewApi, seller_id: str | None, listing_id: str, *, settle_discount: Decimal | None = None,
                         status: str | None = None, weight: int | None = None, actor: str | None = None) -> dict:
    row = await db.fetch_one("SELECT * FROM listings WHERE id = %s", (listing_id,))
    if not row or (seller_id and str(row["seller_id"]) != str(seller_id)) or row["status"] == "removed":
        raise not_found("挂单")
    if settle_discount is not None:
        sell = await _sell_discount(db, row["model_id"])
        if not Decimal("0.05") <= settle_discount < sell:
            raise ApiError(400, "discount_too_high", f"结算折扣要在 0.05 到 {sell} 之间", "settle_discount")
        await db.execute("UPDATE listings SET settle_discount = %s WHERE id = %s", (settle_discount, listing_id))
    if status is not None:
        if status not in ("active", "paused", "removed"):
            raise bad("状态只能是 active / paused / removed", "status")
        await db.execute("UPDATE listings SET status = %s WHERE id = %s", (status, listing_id))
    if weight is not None:
        if not 1 <= weight <= 1000:
            raise bad("权重 1 到 1000", "weight")
        await db.execute("UPDATE listings SET weight = %s WHERE id = %s", (weight, listing_id))
    key = await get_key(db, str(row["key_id"]))
    await _sync_channel(db, gw, key)
    if actor:
        await db.execute("INSERT INTO audit_logs (actor_id, action, target_type, target_id, after) VALUES (%s, 'listing.update', 'listing', %s, %s)",
                         (actor, listing_id, jsonb({"settle_discount": str(settle_discount) if settle_discount is not None else None, "status": status, "weight": weight})))
    return await db.fetch_one("SELECT * FROM listings WHERE id = %s", (listing_id,))


async def market_hint(db: Database, model_id: str) -> dict:
    """给卖方定价参考：这个模型现有挂单的最低/中位结算折扣、平台售价折扣、最近 7 天调用量。"""
    sell = await _sell_discount(db, model_id)
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n, MIN(settle_discount) AS min_discount, percentile_cont(0.5) WITHIN GROUP (ORDER BY settle_discount) AS median_discount "
        "FROM listings WHERE model_id = %s AND status = 'active'", (model_id,))
    req = await db.fetch_val("SELECT COUNT(*) FROM usage_logs WHERE model_id = %s AND created_at >= now() - interval '7 days'", (model_id,))
    return {"model_id": model_id, "sell_discount": sell, "active_listings": row["n"], "min_settle_discount": row["min_discount"],
            "median_settle_discount": row["median_discount"], "requests_7d": req, "suggested_settle_discount": min(Decimal(str(row["min_discount"])) if row["min_discount"] else sell - Decimal("0.1"), sell - Decimal("0.05"))}


def reveal_secret(row: dict) -> str:
    return unseal(row["key_ciphertext"])
