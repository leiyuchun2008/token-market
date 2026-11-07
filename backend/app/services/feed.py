"""首页动态与脱敏：写进 activity_feed 的文字在这里生成，金额只给区间，名字打码。"""
from __future__ import annotations

from decimal import Decimal

from ..db import Database

BANDS = [(100, "100 元以内"), (1000, "100 – 1,000 元"), (10000, "1,000 – 1 万元"), (50000, "1 万 – 5 万元"), (200000, "5 万 – 20 万元")]


def amount_band(cny: Decimal | float) -> str:
    v = float(cny)
    for limit, text in BANDS:
        if v < limit:
            return text
    return "20 万元以上"


def mask(name: str) -> str:
    """'杭州某某科技' -> '杭**科技'；短名字只留第一个字。"""
    n = (name or "").strip()
    if len(n) <= 1:
        return n + "**"
    if len(n) <= 3:
        return n[0] + "**"
    return n[0] + "**" + n[-2:]


async def add(db: Database, kind: str, text: str, *, model_id: str | None = None, band: str | None = None, is_demo: bool = False) -> None:
    await db.execute("INSERT INTO activity_feed (type, display_text, model_id, amount_band, is_demo) VALUES (%s, %s, %s, %s, %s)",
                     (kind, text[:200], model_id, band, is_demo))


async def recent(db: Database, limit: int = 30, include_demo: bool = True) -> list[dict]:
    sql = "SELECT id, type, display_text, model_id, amount_band, is_demo, created_at FROM activity_feed"
    if not include_demo:
        sql += " WHERE NOT is_demo"
    sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
    return await db.fetch_all(sql, (max(1, min(200, limit)),))
