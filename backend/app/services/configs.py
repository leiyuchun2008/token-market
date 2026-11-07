"""system_configs：平台参数（汇率、默认折扣、结算周期……），值是 jsonb。"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..db import Database, jsonb

DEFAULTS: dict[str, Any] = {
    "fx_usd_cny": 7.20, "default_sell_discount": 0.75, "topup_fee_rate": 0, "payout_fee_rate": 0.01,
    "default_settlement_cycle_days": 7, "key_dead_after_failures": 5, "probe_interval_minutes": 10, "reserve_ratio": 1.2,
    "first_topup_bonus_rate": 0.10, "newapi_quota_per_unit": 500000, "min_payout_cny": 100, "sync_interval_seconds": 120,
    "max_sync_pages": 200, "default_channel_group": "default",
}
# 后台可改的键和它们的说明
EDITABLE: dict[str, str] = {
    "fx_usd_cny": "美元兑人民币汇率（官方价换算成买方售价用）",
    "default_sell_discount": "默认售价折扣（0.75 = 官方价 7.5 折）",
    "payout_fee_rate": "提现手续费率",
    "default_settlement_cycle_days": "默认结算周期（天）",
    "key_dead_after_failures": "连续探活失败多少次判定 Key 失效",
    "probe_interval_minutes": "探活间隔（分钟）",
    "first_topup_bonus_rate": "首充赠送比例",
    "min_payout_cny": "最低提现金额（元）",
    "sync_interval_seconds": "从转发引擎同步调用记录的间隔（秒）",
    "default_channel_group": "卖方渠道在转发引擎里的分组",
    "topup_fee_rate": "充值手续费率",
}


async def all_configs(db: Database) -> dict[str, Any]:
    rows = await db.fetch_all("SELECT key, value FROM system_configs")
    out = dict(DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


async def get(db: Database, key: str) -> Any:
    row = await db.fetch_one("SELECT value FROM system_configs WHERE key = %s", (key,))
    return row["value"] if row else DEFAULTS.get(key)


async def dec(db: Database, key: str) -> Decimal:
    v = await get(db, key)
    return Decimal(str(v))


async def num(db: Database, key: str) -> float:
    return float(await get(db, key))


async def set_config(db: Database, key: str, value: Any, actor_id: str | None = None) -> None:
    if key not in EDITABLE:
        raise ValueError(f"不能改的参数：{key}")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    await db.execute(
        "INSERT INTO system_configs (key, value, updated_by, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
        (key, jsonb(value), actor_id))
