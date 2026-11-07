"""模型目录与定价：providers / models / pricing_rules，以及把售价推到转发引擎（ModelRatio 等）。

买方价 = 官方价 × 售价折扣（sell_discount）。New API 的计价参数：
* token 模型：ModelRatio = 每百万 token 输入价（美元）÷ 2（1 ratio = $2/百万 token），CompletionRatio = 输出价 ÷ 输入价；
* 按次/按秒/按张的模型：ModelPrice = 每次价格（美元）；视频按一次 default_video_seconds 秒估价。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ..db import Database
from ..errors import ApiError, bad, not_found
from ..newapi import NewApi
from . import configs

MODEL_ID_RE = r"^[a-z0-9][a-z0-9._-]{1,79}$"
DEFAULT_VIDEO_SECONDS = 5
# New API 渠道类型（constant/channel.go）：按上游 api_style 选默认
CHANNEL_TYPE_BY_STYLE = {"openai": 1, "anthropic": 14, "volc": 45, "bedrock": 33, "gemini": 24}


async def list_providers(db: Database) -> list[dict]:
    return await db.fetch_all("SELECT id, name, base_url, api_style, channel_type, is_demo FROM providers ORDER BY id")


async def upsert_provider(db: Database, *, id: str, name: str, base_url: str | None, api_style: str = "openai", channel_type: int | None = None) -> dict:
    import re
    if not re.match(r"^[a-z][a-z0-9_]{1,39}$", id or ""):
        raise bad("厂商 id 只能是小写字母、数字、下划线", "id")
    if not name or len(name) > 80:
        raise bad("厂商名称 1 到 80 个字", "name")
    ct = channel_type or CHANNEL_TYPE_BY_STYLE.get(api_style, 1)
    await db.execute(
        "INSERT INTO providers (id, name, base_url, api_style, channel_type) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, base_url = EXCLUDED.base_url, api_style = EXCLUDED.api_style, channel_type = EXCLUDED.channel_type",
        (id, name, base_url or None, api_style, ct))
    return await db.fetch_one("SELECT * FROM providers WHERE id = %s", (id,))


async def list_models(db: Database, include_demo: bool = True, status: str | None = "active") -> list[dict]:
    sql = "SELECT * FROM models WHERE TRUE"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if not include_demo:
        sql += " AND NOT is_demo"
    sql += " ORDER BY is_featured DESC, id"
    return await db.fetch_all(sql, params)


async def get_model(db: Database, model_id: str) -> dict:
    row = await db.fetch_one("SELECT * FROM models WHERE id = %s", (model_id,))
    if not row:
        raise not_found("模型")
    return row


async def upsert_model(db: Database, data: dict) -> dict:
    import re
    mid = str(data.get("id") or "").strip()
    if not re.match(MODEL_ID_RE, mid):
        raise bad("模型 id 用小写字母、数字、点、横线，2 到 80 位", "id")
    if not await db.fetch_one("SELECT 1 FROM providers WHERE id = %s", (data.get("provider_id"),)):
        raise bad("厂商不存在", "provider_id")
    kind, mode = data.get("kind"), data.get("billing_mode")
    if kind not in ("text", "video", "image", "audio", "embedding"):
        raise bad("kind 不对", "kind")
    if mode not in ("token", "video_second", "image", "request"):
        raise bad("billing_mode 不对", "billing_mode")
    if mode == "token" and (data.get("official_input_usd") is None or data.get("official_output_usd") is None):
        raise bad("token 计价的模型要填输入和输出的官方价（美元 / 百万 token）", "official_input_usd")
    if mode != "token" and data.get("official_unit_usd") is None:
        raise bad("按次/按秒/按张计价的模型要填单位官方价（美元）", "official_unit_usd")
    await db.execute(
        "INSERT INTO models (id, provider_id, display_name, kind, billing_mode, official_input_usd, official_output_usd, official_unit_usd, upstream_model_name, is_featured, status) "
        "VALUES (%(id)s, %(provider_id)s, %(display_name)s, %(kind)s, %(billing_mode)s, %(official_input_usd)s, %(official_output_usd)s, %(official_unit_usd)s, %(upstream_model_name)s, %(is_featured)s, %(status)s) "
        "ON CONFLICT (id) DO UPDATE SET provider_id = EXCLUDED.provider_id, display_name = EXCLUDED.display_name, kind = EXCLUDED.kind, billing_mode = EXCLUDED.billing_mode, "
        "official_input_usd = EXCLUDED.official_input_usd, official_output_usd = EXCLUDED.official_output_usd, official_unit_usd = EXCLUDED.official_unit_usd, "
        "upstream_model_name = EXCLUDED.upstream_model_name, is_featured = EXCLUDED.is_featured, status = EXCLUDED.status",
        {"id": mid, "provider_id": data["provider_id"], "display_name": (data.get("display_name") or mid)[:120], "kind": kind, "billing_mode": mode,
         "official_input_usd": data.get("official_input_usd"), "official_output_usd": data.get("official_output_usd"), "official_unit_usd": data.get("official_unit_usd"),
         "upstream_model_name": (data.get("upstream_model_name") or mid)[:120], "is_featured": bool(data.get("is_featured")), "status": data.get("status") or "active"})
    return await get_model(db, mid)


async def sell_discount(db: Database, model_id: str, buyer_tier: int | None = None) -> Decimal:
    """取最具体的一条生效售价规则（模型专属 > 全局；优先级大的先），没有就用默认折扣。"""
    row = await db.fetch_one(
        "SELECT sell_discount FROM pricing_rules WHERE (model_id = %s OR model_id IS NULL) AND (buyer_tier IS NULL OR buyer_tier = %s) "
        "AND effective_from <= now() AND (effective_to IS NULL OR effective_to > now()) "
        "ORDER BY model_id NULLS LAST, buyer_tier NULLS LAST, priority DESC LIMIT 1", (model_id, buyer_tier))
    return Decimal(str(row["sell_discount"])) if row else await configs.dec(db, "default_sell_discount")


async def list_rules(db: Database) -> list[dict]:
    return await db.fetch_all("SELECT * FROM pricing_rules ORDER BY model_id NULLS FIRST, priority DESC, effective_from DESC")


async def add_rule(db: Database, *, model_id: str | None, sell_discount: Decimal, buyer_tier: int | None = None, priority: int = 0, note: str | None = None) -> dict:
    if not Decimal("0.05") <= sell_discount <= Decimal("2"):
        raise bad("售价折扣要在 0.05 到 2 之间（0.7 = 7 折）", "sell_discount")
    if model_id:
        await get_model(db, model_id)
    row = await db.fetch_one(
        "INSERT INTO pricing_rules (model_id, buyer_tier, sell_discount, priority, note) VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (model_id, buyer_tier, sell_discount, priority, note))
    return row


async def delete_rule(db: Database, rule_id: str) -> None:
    if not await db.execute("DELETE FROM pricing_rules WHERE id = %s", (rule_id,)):
        raise not_found("定价规则")


def buyer_prices(model: dict, discount: Decimal, fx: Decimal) -> dict:
    """一个模型对买方的售价（美元和人民币），给市场页和定价同步用。"""
    out = {"sell_discount": discount}
    if model["billing_mode"] == "token":
        i = Decimal(str(model["official_input_usd"] or 0)) * discount
        o = Decimal(str(model["official_output_usd"] or 0)) * discount
        out.update(input_usd_per_m=i, output_usd_per_m=o, input_cny_per_m=i * fx, output_cny_per_m=o * fx)
    else:
        u = Decimal(str(model["official_unit_usd"] or 0)) * discount
        out.update(unit_usd=u, unit_cny=u * fx)
    return out


async def newapi_pricing(db: Database) -> dict[str, str]:
    """算出要写进转发引擎的三个选项（JSON 文本）。"""
    ratio: dict[str, float] = {}
    completion: dict[str, float] = {}
    price: dict[str, float] = {}
    for m in await list_models(db):
        d = await sell_discount(db, m["id"])
        if m["billing_mode"] == "token":
            i = Decimal(str(m["official_input_usd"] or 0)) * d
            o = Decimal(str(m["official_output_usd"] or 0)) * d
            ratio[m["id"]] = float(round(i / 2, 6))
            completion[m["id"]] = float(round(o / i, 4)) if i > 0 else 1.0
        else:
            u = Decimal(str(m["official_unit_usd"] or 0)) * d
            units = DEFAULT_VIDEO_SECONDS if m["billing_mode"] == "video_second" else 1
            price[m["id"]] = float(round(u * units, 6))
    return {"ModelRatio": json.dumps(ratio), "CompletionRatio": json.dumps(completion), "ModelPrice": json.dumps(price)}


async def sync_pricing(db: Database, gw: NewApi) -> dict[str, str]:
    """把售价写进转发引擎（整份覆盖这三个选项：目录以平台为准）。"""
    if not gw.configured:
        raise ApiError(503, "gateway_unconfigured", "转发引擎还没接入")
    opts = await newapi_pricing(db)
    for k, v in opts.items():
        await gw.set_option(k, v)
    return opts
