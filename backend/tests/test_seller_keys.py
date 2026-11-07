"""Key 托管 → 渠道；挂单 → 渠道模型列表；暂停 / 删除 / 探活失败自动下架 / 恢复。"""
from __future__ import annotations

import uuid

from app.services import keys as keys_svc


def channel(env, cid):
    return next(c for c in env.fake.channels if c["id"] == cid)


async def test_key_upload_creates_disabled_channel(env):
    _reg, h = await env.approved_seller()
    r = await env.client.post("/api/seller/keys", json={"provider_id": "bytedance_volc", "secret": "sk-good-abcdef123456", "label": "主账号"}, headers=h)
    assert r.status_code == 202, r.text
    k = r.json()
    assert k["status"] == "active" and k["channel_name"] == f"mk-{uuid.UUID(_reg['user']['id']).hex[:6]}-1"
    assert "key_ciphertext" not in k and k["key_hint"] == "****3456" and "secret" not in k
    assert [m["id"] for m in k["suggested_models"]] == ["doubao-seed-1-6", "seedance-1-0-pro"]   # 校验时上游返回了这两个模型
    ch = channel(env, k["channel_id"])
    assert ch["status"] == 2 and ch["key"] == "sk-good-abcdef123456" and ch["type"] == 45 and ch["group"] == "default"
    # 列表里看得到，详情里有校验记录
    r = await env.client.get("/api/seller/keys", headers=h)
    assert len(r.json()) == 1 and r.json()[0]["active_listings"] == 0
    r = await env.client.get(f"/api/seller/keys/{k['id']}", headers=h)
    assert r.json()["recent_probes"][0]["error_code"] == "validated"
    # 同一个 Key 不能重复托管
    r = await env.client.post("/api/seller/keys", json={"provider_id": "bytedance_volc", "secret": "sk-good-abcdef123456"}, headers=h)
    assert r.status_code == 409
    # 无效 Key：引擎列不出模型 → 拒收，不建渠道
    r = await env.client.post("/api/seller/keys", json={"provider_id": "bytedance_volc", "secret": "sk-bad-abcdef123456"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_invalid"
    assert len(env.fake.channels) == 1


async def test_listing_drives_channel(env):
    _reg, h = await env.approved_seller()
    key, listing = await env.listed_key(h, settle="0.60")
    ch = channel(env, key["channel_id"])
    assert ch["status"] == 1 and ch["models"] == "doubao-seed-1-6" and ch["priority"] == 40 and ch["model_mapping"] == ""
    # 第二个模型：上游名不同 → 映射
    r = await env.client.post("/api/seller/listings", json={"key_id": key["id"], "model_id": "seedance-1-0-pro", "settle_discount": "0.50"}, headers=h)
    assert r.status_code == 201, r.text
    ch = channel(env, key["channel_id"])
    assert set(ch["models"].split(",")) == {"doubao-seed-1-6", "seedance-1-0-pro"} and ch["priority"] == 50
    assert '"seedance-1-0-pro": "doubao-seedance-1-0-pro"' in ch["model_mapping"]
    # 结算折扣不能高于平台售价折扣（0.75）
    r = await env.client.patch(f"/api/seller/listings/{listing['id']}", json={"settle_discount": "0.80"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "discount_too_high"
    # 重复挂单
    r = await env.client.post("/api/seller/listings", json={"key_id": key["id"], "model_id": "doubao-seed-1-6", "settle_discount": "0.5"}, headers=h)
    assert r.status_code == 409
    # 暂停一个挂单 → 渠道模型列表少一个；全部暂停 → 渠道停用
    r = await env.client.patch(f"/api/seller/listings/{listing['id']}", json={"status": "paused"}, headers=h)
    assert r.status_code == 200
    assert channel(env, key["channel_id"])["models"] == "seedance-1-0-pro"
    r = await env.client.get("/api/seller/listings", headers=h)
    assert sorted(x["status"] for x in r.json()) == ["active", "paused"]
    r2 = await env.client.get("/api/seller/market-hint", params={"model_id": "seedance-1-0-pro"}, headers=h)
    assert r2.json()["active_listings"] == 1 and r2.json()["min_settle_discount"] == "0.5"
    other = [x for x in r.json() if x["status"] == "active"][0]
    r = await env.client.delete(f"/api/seller/listings/{other['id']}", headers=h)
    assert r.status_code == 204
    assert channel(env, key["channel_id"])["status"] == 2
    # Key 暂停 / 恢复 / 删除
    r = await env.client.patch(f"/api/seller/listings/{listing['id']}", json={"status": "active"}, headers=h)
    assert channel(env, key["channel_id"])["status"] == 1
    r = await env.client.patch(f"/api/seller/keys/{key['id']}", json={"status": "paused"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "paused" and channel(env, key["channel_id"])["status"] == 2
    r = await env.client.patch(f"/api/seller/keys/{key['id']}", json={"status": "active"}, headers=h)
    assert channel(env, key["channel_id"])["status"] == 1
    r = await env.client.delete(f"/api/seller/keys/{key['id']}", headers=h)
    assert r.status_code == 204 and env.fake.channels == []
    r = await env.client.get("/api/seller/keys", headers=h)
    assert r.json() == []
    # 公开市场页：没有在线卖方了
    r = await env.client.get("/api/public/models")
    assert all(m["online_sellers"] == 0 for m in r.json())


async def test_probe_marks_dead_and_revives(env):
    reg, h = await env.approved_seller()
    key, listing = await env.listed_key(h)
    env.fake.channel_test_fail.add(key["channel_id"])
    for i in range(3):   # key_dead_after_failures = 3
        out = await keys_svc.probe_all(env.db, env.gw)
    assert out["died"] == [key["id"]]
    r = await env.client.get(f"/api/seller/keys/{key['id']}", headers=h)
    k = r.json()
    assert k["status"] == "dead" and k["consecutive_failures"] == 3 and k["listings"][0]["status"] == "paused"
    assert channel(env, key["channel_id"])["status"] == 2
    r = await env.client.get("/api/auth/me", headers=h)
    assert r.json()["user"]["credit_score"] == 50   # 60 - 10
    # 失效的 Key 不能直接改回 active，要重新校验
    r = await env.client.patch(f"/api/seller/keys/{key['id']}", json={"status": "active"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_dead"
    r = await env.client.post(f"/api/seller/keys/{key['id']}/revalidate", headers=h)
    assert r.json()["revalidation"]["ok"] is False and r.json()["status"] == "dead"
    # 上游恢复后：重新校验通过 → 恢复 active，挂单恢复，渠道启用
    env.fake.channel_test_fail.clear()
    r = await env.client.post(f"/api/seller/keys/{key['id']}/revalidate", headers=h)
    assert r.json()["revalidation"]["ok"] is True and r.json()["status"] == "active" and r.json()["listings"][0]["status"] == "active"
    assert channel(env, key["channel_id"])["status"] == 1
    # 后台也能看全部 Key
    adm = await env.admin()
    r = await env.client.get("/api/admin/keys", headers=adm)
    assert r.json()[0]["seller_name"] == "星河算力" and r.json()[0]["success_rate_7d"] is not None
