"""完整一圈：定价同步 → 买方充值 → 建子密钥 → 调用 → 同步用量 → 扣余额 / 卖方收益 → 结算 → 提现。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from app.services import settlements as settle_svc


async def test_topup_and_token(env):
    reg, h = await env.funded_buyer(amount="1000")
    r = await env.client.get("/api/buyer/wallet", headers=h)
    assert r.json()["balance"] == "1100"      # 首充送 10%
    r = await env.client.get("/api/buyer/transactions", headers=h)
    assert [t["type"] for t in r.json()] == ["bonus", "topup"]
    # 引擎里的额度：1100 元 ÷ 7.2 × 500000
    u = next(x for x in env.fake.users if x["role"] == 1)
    assert u["quota"] == round(1100 / 7.2 * 500000)
    r = await env.client.get("/api/public/feed")
    assert any(x["type"] == "topup" and "1,000 – 1 万元" in x["display_text"] for x in r.json())
    # 子密钥
    r = await env.client.post("/api/buyer/tokens", json={"name": "生产", "allowed_models": ["doubao-seed-1-6"]}, headers=h)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["token"].startswith("sk-") and t["token_prefix"] == t["token"][:12] and t["api_base"] == "https://api.test/v1"
    ft = env.fake.tokens[0]
    assert ft["model_limits"] == "doubao-seed-1-6" and ft["unlimited_quota"] and ft["user_id"] == u["id"]
    r = await env.client.get("/api/buyer/tokens", headers=h)
    assert len(r.json()["items"]) == 1 and "token" not in r.json()["items"][0]
    r = await env.client.post("/api/buyer/tokens", json={"name": "x", "allowed_models": ["nope"]}, headers=h)
    assert r.status_code == 400
    r = await env.client.patch(f"/api/buyer/tokens/{t['id']}", json={"status": "disabled"}, headers=h)
    assert r.json()["status"] == "disabled" and env.fake.tokens[0]["status"] == 2
    r = await env.client.delete(f"/api/buyer/tokens/{t['id']}", headers=h)
    assert r.status_code == 204 and env.fake.tokens == []
    # 审核拒绝的充值不入账
    r = await env.client.post("/api/buyer/topups", json={"amount": "500", "pay_method": "bank"}, headers=h)
    adm = await env.admin()
    r = await env.client.post(f"/api/admin/topups/{r.json()['id']}/review", json={"approve": False, "note": "没收到款"}, headers=adm)
    assert r.json()["status"] == "rejected"
    r = await env.client.get("/api/buyer/wallet", headers=h)
    assert r.json()["balance"] == "1100"


async def test_full_trade_loop(env):
    adm = await env.admin()
    # 1. 售价推到引擎：Doubao 1.00/4.00 美元 × 0.75 → ratio 0.375、completion 4；Seedance 0.08 × 0.75 × 5 秒 = 0.3 美元/次
    r = await env.client.post("/api/admin/pricing/sync", headers=adm)
    assert r.status_code == 200, r.text
    assert json.loads(env.fake.options["ModelRatio"]) == {"doubao-seed-1-6": 0.375}
    assert json.loads(env.fake.options["CompletionRatio"]) == {"doubao-seed-1-6": 4.0}
    assert json.loads(env.fake.options["ModelPrice"]) == {"seedance-1-0-pro": 0.3}
    # 2. 卖方挂单，买方充值并建密钥
    sreg, sh = await env.approved_seller()
    key, listing = await env.listed_key(sh, settle="0.60")
    r = await env.client.post("/api/seller/listings", json={"key_id": key["id"], "model_id": "seedance-1-0-pro", "settle_discount": "0.50"}, headers=sh)
    assert r.status_code == 201, r.text
    breg, bh = await env.funded_buyer(amount="100")
    r = await env.client.post("/api/buyer/tokens", json={"name": "默认"}, headers=bh)
    sk = r.json()["token"]
    # 3. 买方调用（1000 输入 + 500 输出）：引擎扣 0.375 × (1000 + 500 × 4) = 1125 额度
    r = await env.call_model(sk, "doubao-seed-1-6", 1000, 500)
    assert r.status_code == 200, r.text
    r = await env.call_model(sk, "seedance-1-0-pro", 10, 0)
    assert r.status_code == 200, r.text
    assert len(env.fake.logs) >= 2
    # 4. 同步用量
    r = await env.client.post("/api/admin/jobs/sync-usage", headers=adm)
    assert r.status_code == 200, r.text
    assert r.json()["synced"] == 2 and r.json()["unmatched"] == 0
    r = await env.client.post("/api/admin/jobs/sync-usage", headers=adm)
    assert r.json()["synced"] == 0   # 游标：不重复
    # 买方：1125 额度 = 1125/500000 × 7.2 = 0.0162 元；Seedance 0.3 美元 = 150000 额度 = 2.16 元
    r = await env.client.get("/api/buyer/usage/summary", headers=bh)
    s = r.json()
    assert s["total_requests"] == 2
    assert Decimal(s["total_cost_cny"]) == Decimal("2.1762")
    r = await env.client.get("/api/buyer/wallet", headers=bh)
    assert Decimal(r.json()["balance"]) == Decimal("110") - Decimal("2.1762")
    r = await env.client.get("/api/buyer/usage", headers=bh)
    assert {x["model_id"] for x in r.json()} == {"doubao-seed-1-6", "seedance-1-0-pro"} and "seller_id" not in r.json()[0]
    # 卖方：Doubao 官方价 (1000×1 + 500×4)/1e6 = 0.003 美元 × 7.2 × 0.6 = 0.01296 元；
    #       Seedance 按次：买方付 2.16 元 ÷ 7.2 ÷ 0.75 = 0.4 美元官方价 × 7.2 × 0.5 = 1.44 元
    r = await env.client.get("/api/seller/earnings", headers=sh)
    e = r.json()
    assert Decimal(e["today_cny"]) == Decimal("1.45296") and e["requests_24h"] == 2 and Decimal(e["unsettled_cny"]) == Decimal("1.45296")
    r = await env.client.get("/api/seller/usage", headers=sh)
    assert "buyer_id" not in json.dumps(r.json())
    # 后台看板：毛利 = 买方付的 − 卖方得的
    r = await env.client.get("/api/admin/dashboard", headers=adm)
    d = r.json()
    assert d["requests_24h"] == 2 and Decimal(d["gmv_24h"]) == Decimal("2.1762") and Decimal(d["margin_24h"]) == Decimal("2.1762") - Decimal("1.45296")
    assert d["gateway"]["ok"] and d["usage_cursor"] >= 2
    # 公开统计
    r = await env.client.get("/api/public/stats")
    assert r.json()["today_requests"] == 2 and r.json()["online_sellers"] == 1 and r.json()["includes_demo"] is False
    r = await env.client.get("/api/public/models/doubao-seed-1-6")
    assert r.json()["online_sellers"] == 1 and r.json()["requests_7d"] == 1 and r.json()["prices"]["input_cny_per_m"] == "5.4"
    # 对账：平台余额 vs 引擎额度 一致
    r = await env.client.get(f"/api/admin/users/{breg['user']['id']}", headers=adm)
    assert Decimal(r.json()["reconcile"]["diff_cny"]) == Decimal("0")
    # 5. 结算：周期 7 天，假装今天是 30 天后
    out = await settle_svc.run(env.db, date.today() + timedelta(days=30))
    assert out["created"] == 1
    r = await env.client.get("/api/seller/settlements", headers=sh)
    st = r.json()[0]
    # 毛收益 1.45（分以下四舍五入），提现手续费预提 1% = 0.01，应付 1.44
    assert st["status"] == "closed" and Decimal(st["gross_earning"]) == Decimal("1.45") and Decimal(st["fee"]) == Decimal("0.01") and Decimal(st["payable"]) == Decimal("1.44")
    r = await env.client.get(f"/api/seller/settlements/{st['id']}", headers=sh)
    assert r.json()["by_model"][0]["model_id"] == "seedance-1-0-pro"
    # 到期 → 可提现
    await env.db.execute("UPDATE settlements SET payable_at = now() - interval '1 minute'")
    out = await settle_svc.run(env.db, date.today() + timedelta(days=30))
    assert out["matured"] == 1
    r = await env.client.get("/api/seller/earnings", headers=sh)
    assert Decimal(r.json()["pending_payout_cny"]) == Decimal("1.44")
    # 6. 提现：低于最低额（100 元）→ 拒；把最低额改成 0.01 后申请 → 后台打款
    r = await env.client.post("/api/seller/payouts", json={"amount": "1.44"}, headers=sh)
    assert r.status_code == 400
    r = await env.client.patch("/api/admin/configs", json={"min_payout_cny": 0.01}, headers=adm)
    assert r.status_code == 200
    r = await env.client.post("/api/seller/payouts", json={"amount": "2"}, headers=sh)
    assert r.status_code == 400 and r.json()["error"]["code"] == "insufficient_payout"
    r = await env.client.post("/api/seller/payouts", json={"amount": "1.44"}, headers=sh)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await env.client.get(f"/api/admin/payouts/{pid}", headers=adm)
    assert r.json()["payout_method"]["account"] == "13800000000"   # 后台看得到完整收款账号
    r = await env.client.get("/api/seller/profile", headers=sh)
    assert r.json()["payout_method"]["account"] == "138****0000"    # 卖方自己看到的是打码的
    r = await env.client.post(f"/api/admin/payouts/{pid}/review", json={"approve": True, "proof_url": "https://x/proof.png"}, headers=adm)
    assert r.json()["status"] == "paid"
    r = await env.client.get("/api/seller/payouts", headers=sh)
    assert r.json()[0]["status"] == "paid" and r.json()[0]["method_type"] == "alipay"
    r = await env.client.get("/api/auth/me", headers=sh)
    assert r.json()["wallet"]["pending_payout"] == "0" and r.json()["user"]["credit_score"] == 61
    # 保证金登记
    r = await env.client.post(f"/api/admin/sellers/{sreg['user']['id']}/deposit", json={"amount": "5000", "note": "银行转账"}, headers=adm)
    assert r.json()["deposit_balance"] == "5000"
    r = await env.client.get("/api/seller/deposit", headers=sh)
    assert r.json()["deposit_balance"] == "5000" and r.json()["transactions"][0]["type"] == "deposit_in"
