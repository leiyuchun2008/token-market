"""PostgreSQL：连接池、迁移、查询小工具。SQL 直接写，不用 ORM（表结构以 migrations/*.sql 为准）。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10):
        self.url = url
        self.pool = AsyncConnectionPool(url, min_size=min_size, max_size=max_size, open=False, kwargs={"row_factory": dict_row})

    async def open(self) -> None:
        await self.pool.open()
        await self.pool.wait()

    async def close(self) -> None:
        await self.pool.close()

    def connection(self):
        """`async with db.connection() as conn:` 一个事务；正常退出提交，异常回滚。"""
        return self.pool.connection()

    # ---- 便捷查询（各自独立的短事务） ----
    async def fetch_one(self, sql: str, params: Any = None) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def execute(self, sql: str, params: Any = None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def fetch_val(self, sql: str, params: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))


async def migrate(url: str) -> list[str]:
    """按文件名顺序执行 migrations/*.sql，记录在 schema_migrations；返回这次新执行的文件名。"""
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        cur = await conn.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in await cur.fetchall()}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            sql = f.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


def jsonb(v: Any) -> Jsonb:
    return Jsonb(v)


def plain(v: Any) -> Any:
    """把数据库行里的 Decimal/UUID/时间等变成可 JSON 序列化的值（金额转成字符串保留精度）。"""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")   # 不用科学计数法：5E+3 -> 5000
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if type(v).__name__ == "UUID":
        return str(v)
    return v


def dumps(v: Any) -> str:
    return json.dumps(plain(v), ensure_ascii=False)
