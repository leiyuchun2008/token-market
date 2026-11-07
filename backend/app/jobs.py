"""后台任务（和 API 同一个进程，单实例部署够用）：
* 每 sync_interval_seconds 从引擎同步调用记录；
* 每 probe_interval_minutes 探活一轮 Key；
* 每天 02:00（北京时间）跑结算，归零 Key 的当日用量。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .db import Database
from .newapi import NewApi
from .services import configs, keys, settlements, usage

log = logging.getLogger("token-market.jobs")
SH = timezone(timedelta(hours=8))


async def run_jobs(db: Database, gw: NewApi) -> None:
    await asyncio.sleep(5)
    last_sync = last_probe = 0.0
    last_settle_day = None
    while True:
        try:
            now = asyncio.get_event_loop().time()
            sync_every = max(15, int(await configs.num(db, "sync_interval_seconds")))
            probe_every = max(1, int(await configs.num(db, "probe_interval_minutes"))) * 60
            if now - last_sync >= sync_every:
                last_sync = now
                r = await usage.sync(db, gw)
                if r.get("synced"):
                    log.info("usage sync: %s", r)
            if now - last_probe >= probe_every:
                last_probe = now
                r = await keys.probe_all(db, gw)
                if r.get("died") or r.get("revived"):
                    log.info("probe: %s", r)
            sh_now = datetime.now(SH)
            if sh_now.hour >= 2 and last_settle_day != sh_now.date():
                last_settle_day = sh_now.date()
                r = await settlements.run(db, sh_now.date())
                await usage.reset_daily_caps(db)
                log.info("settlements: %s", r)
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001 — 后台循环不能因为一次失败停掉
            log.exception("job failed")
        await asyncio.sleep(10)
