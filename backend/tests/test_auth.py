"""注册、登录、入驻审核。"""
from __future__ import annotations


async def test_register_login_me(env):
    reg = await env.register("杭州某科技", email="buyer@test.com")
    assert reg["user"]["role"] == "buyer" and reg["token"]
    # 转发引擎里同步建了用户（密码由平台保管）
    assert any(u["username"].startswith("u") and u["role"] == 1 for u in env.fake.users)
    r = await env.client.get("/api/auth/me", headers=env.auth(reg))
    assert r.status_code == 200
    me = r.json()
    assert me["user"]["nickname"] == "杭州某科技" and me["wallet"]["balance"] == "0" and me["is_buyer"] and not me["is_seller"] and not me["is_admin"]
    # 登录
    r = await env.client.post("/api/auth/login", json={"account": "buyer@test.com", "password": "Passw0rd!x"})
    assert r.status_code == 200 and r.json()["token"]
    r = await env.client.post("/api/auth/login", json={"account": "buyer@test.com", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_login"
    # 重复邮箱
    r = await env.client.post("/api/auth/register", json={"nickname": "另一个", "email": "buyer@test.com", "password": "Passw0rd!x"})
    assert r.status_code == 409
    # 没登录
    r = await env.client.get("/api/auth/me")
    assert r.status_code == 401
    # 首页动态：加入了平台（昵称打码）
    r = await env.client.get("/api/public/feed")
    assert any("杭**科技" in x["display_text"] for x in r.json())


async def test_seller_apply_and_review(env):
    reg = await env.register("星河算力", role="seller", key_source_note="自有火山引擎企业账号，长期稳定供应 Seedance")
    h = env.auth(reg)
    # 还没审核：卖方接口进不去
    r = await env.client.get("/api/seller/keys", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_seller"
    r = await env.client.get("/api/seller/profile", headers=h)
    assert r.json()["review_status"] == "pending"
    # 申请内容太短
    r = await env.client.post("/api/seller/apply", json={"key_source_note": "短"}, headers=h)
    assert r.status_code == 400
    adm = await env.admin()
    r = await env.client.get("/api/admin/sellers/pending", headers=adm)
    assert [x["nickname"] for x in r.json()] == ["星河算力"]
    # 普通用户不能审核
    r = await env.client.post(f"/api/admin/sellers/{reg['user']['id']}/review", json={"approve": True}, headers=h)
    assert r.status_code == 403
    r = await env.client.post(f"/api/admin/sellers/{reg['user']['id']}/review", json={"approve": True, "settlement_cycle_days": 3, "deposit_required": "5000"}, headers=adm)
    assert r.status_code == 200
    p = r.json()
    assert p["review_status"] == "approved" and p["settlement_cycle_days"] == 3 and p["deposit_required"] == "5000"
    r = await env.client.get("/api/auth/me", headers=h)
    assert r.json()["user"]["role"] == "both" and r.json()["is_seller"]
    r = await env.client.get("/api/seller/keys", headers=h)
    assert r.status_code == 200 and r.json() == []
    r = await env.client.get("/api/public/feed")
    assert any(x["type"] == "seller_joined" and "星**算力" in x["display_text"] for x in r.json())


async def test_seller_reject(env):
    reg = await env.register("差评供应商", role="seller", key_source_note="来源不明的一批 Key，先试试看能不能卖")
    adm = await env.admin()
    r = await env.client.post(f"/api/admin/sellers/{reg['user']['id']}/review", json={"approve": False, "note": "来源说不清"}, headers=adm)
    assert r.status_code == 200 and r.json()["review_status"] == "rejected"
    r = await env.client.get("/api/seller/keys", headers=env.auth(reg))
    assert r.status_code == 403
    r = await env.client.get("/api/admin/audit-logs", headers=adm)
    assert r.json()[0]["action"] == "seller.reject"
