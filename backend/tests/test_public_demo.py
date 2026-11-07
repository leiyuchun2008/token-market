"""公开接口与演示数据：装上有「演示」标记，卸掉一条不剩。"""
from __future__ import annotations


async def test_public_without_data(env):
    r = await env.client.get("/api/public/stats")
    assert r.status_code == 200 and r.json()["online_models"] == 0 and r.json()["includes_demo"] is False
    r = await env.client.get("/api/public/models")
    assert [m["id"] for m in r.json()] == ["doubao-seed-1-6", "seedance-1-0-pro"]
    m = r.json()[0]
    assert m["prices"]["input_cny_per_m"] == "5.4" and m["prices"]["output_cny_per_m"] == "21.6" and m["online_sellers"] == 0
    r = await env.client.get("/api/public/models/nope")
    assert r.status_code == 404
    r = await env.client.get("/api/public/models/doubao-seed-1-6/health")
    assert r.json()["online_keys"] == 0
    r = await env.client.get("/api/public/feed")
    assert r.json() == []
    r = await env.client.get("/api/public/demands")
    assert r.json() == []


async def test_demo_seed_and_clear(env):
    adm = await env.admin()
    r = await env.client.post("/api/admin/demo/seed", headers=adm)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["users"] >= 50 and c["usage_logs"] > 1000 and c["feed"] > 0
    r = await env.client.get("/api/public/stats")
    assert r.json()["includes_demo"] is True and r.json()["online_sellers"] > 0
    r = await env.client.get("/api/public/feed", params={"limit": 50})
    assert r.json() and all(x["is_demo"] for x in r.json())
    r = await env.client.get("/api/public/models")
    assert any(m["is_demo"] and m["online_sellers"] > 0 for m in r.json())
    r = await env.client.get("/api/public/demands")
    assert r.json() and "budget_cny" not in r.json()[0] and r.json()[0]["budget_band"]
    # 后台看得到演示用户，可筛
    r = await env.client.get("/api/admin/users", params={"q": "演示"}, headers=adm)
    assert r.json() and all(u["is_demo"] for u in r.json())
    # 卸载
    r = await env.client.post("/api/admin/demo/clear", headers=adm)
    assert r.json() == {"users": 0, "usage_logs": 0, "feed": 0, "models": 0}
    r = await env.client.get("/api/public/stats")
    assert r.json()["includes_demo"] is False
    r = await env.client.get("/api/public/models")
    assert all(not m["is_demo"] for m in r.json())   # 非演示的目录保留


async def test_admin_feed_post_marks_demo(env):
    adm = await env.admin()
    r = await env.client.post("/api/admin/feed", json={"type": "topup", "display_text": "买家 深**科技 充值了 1 万 – 5 万元"}, headers=adm)
    assert r.status_code == 201
    r = await env.client.get("/api/public/feed")
    assert r.json()[0]["is_demo"] is True
    r = await env.client.delete(f"/api/admin/feed/{r.json()[0]['id']}", headers=adm)
    assert r.status_code == 204
