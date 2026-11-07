"""演示数据：装（demo/seed_demo.sql）和卸（demo/cleanup_demo.sql）。全部行 is_demo = true，昵称「演示·」开头。"""
from __future__ import annotations

from pathlib import Path

import psycopg

DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "demo"


async def _run_sql(url: str, name: str) -> None:
    sql = (DEMO_DIR / name).read_text(encoding="utf-8")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(sql)


async def seed(url: str) -> dict:
    await _run_sql(url, "seed_demo.sql")
    return await counts(url)


async def clear(url: str) -> dict:
    await _run_sql(url, "cleanup_demo.sql")
    return await counts(url)


async def counts(url: str) -> dict:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(
            "SELECT (SELECT COUNT(*) FROM users WHERE is_demo) AS users, (SELECT COUNT(*) FROM usage_logs WHERE is_demo) AS usage_logs, "
            "(SELECT COUNT(*) FROM activity_feed WHERE is_demo) AS feed, (SELECT COUNT(*) FROM models WHERE is_demo) AS models")
        r = await cur.fetchone()
        return {"users": r[0], "usage_logs": r[1], "feed": r[2], "models": r[3]}
