"""命令行：python -m app.cli <命令>

  migrate                      执行数据库迁移
  create-admin <邮箱> <密码>    建第一个管理员（已存在则改密码并提升为管理员）
  seed-demo / clear-demo       装 / 卸演示数据
  sync-usage / probe-keys / settle   手工跑一次后台任务
  sync-pricing                 把售价推到转发引擎
  gateway-status               看引擎连通情况
"""
from __future__ import annotations

import asyncio
import sys

from .config import settings
from .db import Database, dumps, migrate
from .newapi import NewApi
from .security import hash_password
from .services import catalog, demo, keys, settlements, usage


async def _with_db(fn):
    db = Database(settings.database_url, max_size=2)
    await db.open()
    gw = NewApi(settings.newapi_base_url, settings.newapi_token)
    try:
        return await fn(db, gw)
    finally:
        await gw.aclose()
        await db.close()


async def create_admin(email: str, password: str) -> None:
    async def go(db: Database, _gw: NewApi):
        row = await db.fetch_one("SELECT id FROM users WHERE email = %s", (email.lower(),))
        if row:
            await db.execute("UPDATE users SET role = 'admin', status = 'active', password_hash = %s, updated_at = now() WHERE id = %s", (hash_password(password), row["id"]))
            print(f"已把 {email} 设为管理员并更新密码")
        else:
            r = await db.fetch_one("INSERT INTO users (role, nickname, email, password_hash, credit_score) VALUES ('admin', '平台管理员', %s, %s, 100) RETURNING id", (email.lower(), hash_password(password)))
            await db.execute("INSERT INTO wallets (user_id) VALUES (%s)", (r["id"],))
            print(f"已创建管理员 {email}")
    await _with_db(go)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "migrate":
        applied = asyncio.run(migrate(settings.database_url))
        print("已执行：" + (", ".join(applied) if applied else "无（已是最新）"))
    elif cmd == "create-admin":
        if len(args) != 2 or len(args[1]) < 8:
            print("用法：create-admin <邮箱> <密码（至少 8 位）>")
            return 1
        asyncio.run(create_admin(args[0], args[1]))
    elif cmd == "seed-demo":
        if settings.env == "prod":
            print("生产环境不能装演示数据")
            return 1
        print(dumps(asyncio.run(demo.seed(settings.database_url))))
    elif cmd == "clear-demo":
        print(dumps(asyncio.run(demo.clear(settings.database_url))))
    elif cmd == "sync-usage":
        print(dumps(asyncio.run(_with_db(lambda db, gw: usage.sync(db, gw)))))
    elif cmd == "probe-keys":
        print(dumps(asyncio.run(_with_db(lambda db, gw: keys.probe_all(db, gw)))))
    elif cmd == "settle":
        print(dumps(asyncio.run(_with_db(lambda db, _gw: settlements.run(db)))))
    elif cmd == "sync-pricing":
        print(dumps(asyncio.run(_with_db(lambda db, gw: catalog.sync_pricing(db, gw)))))
    elif cmd == "gateway-status":
        async def st(_db, gw: NewApi):
            if not gw.configured:
                return {"configured": False}
            return {"configured": True, **(await gw.status())}
        print(dumps(asyncio.run(_with_db(st))))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
