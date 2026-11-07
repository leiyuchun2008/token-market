"""测试环境：一个干净的 PostgreSQL 库（TEST_DATABASE_URL，默认本机 5433）+ 内存里的仿 New API + 我们的应用。"""
from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import httpx
import psycopg
import pytest
import pytest_asyncio

os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-0000")
os.environ.setdefault("NEWAPI_BASE_URL", "http://fake-newapi")
os.environ.setdefault("NEWAPI_TOKEN", "pat-root")
os.environ.setdefault("JOBS_ENABLED", "false")
os.environ.setdefault("ENV", "test")

from app.config import Settings  # noqa: E402
from app.db import migrate  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_password  # noqa: E402
from tests.fake_newapi import FakeState, make_fake_newapi, seed_user  # noqa: E402

ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres@127.0.0.1:5433/postgres")
TEST_DB = "token_market_test"


def _test_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_url()
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    asyncio.run(migrate(url))
    return url


TABLES = ["audit_logs", "activity_feed", "demand_requests", "delivery_orders", "disputes", "credit_events", "deductions", "payout_requests", "settlements",
          "usage_hourly", "usage_logs", "wallet_transactions", "topup_orders", "api_tokens", "key_probes", "listings", "provider_keys", "hardware_listings",
          "pricing_rules", "wallets", "seller_profiles", "users", "models", "providers", "sync_state"]


@pytest.fixture(autouse=True)
def clean_db(database_url: str):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute("DELETE FROM system_configs")
        conn.execute("INSERT INTO system_configs (key, value) VALUES ('fx_usd_cny', '7.20'), ('default_sell_discount', '0.75'), ('payout_fee_rate', '0.01'), "
                     "('default_settlement_cycle_days', '7'), ('key_dead_after_failures', '3'), ('first_topup_bonus_rate', '0.10'), ('min_payout_cny', '100'), ('newapi_quota_per_unit', '500000')")
        # 一个厂商、两个模型（token 计价 + 按次计价）
        conn.execute("INSERT INTO providers (id, name, base_url, api_style, channel_type) VALUES ('bytedance_volc', '火山引擎', 'https://ark.example.com/api/v3', 'volc', 45)")
        conn.execute("INSERT INTO models (id, provider_id, display_name, kind, billing_mode, official_input_usd, official_output_usd, upstream_model_name, is_featured) "
                     "VALUES ('doubao-seed-1-6', 'bytedance_volc', 'Doubao Seed 1.6', 'text', 'token', 1.00, 4.00, 'doubao-seed-1-6', true)")
        conn.execute("INSERT INTO models (id, provider_id, display_name, kind, billing_mode, official_unit_usd, upstream_model_name) "
                     "VALUES ('seedance-1-0-pro', 'bytedance_volc', 'Seedance 1.0 Pro', 'video', 'video_second', 0.08, 'doubao-seedance-1-0-pro')")
    yield


@pytest_asyncio.fixture
async def env(database_url: str):
    state = FakeState()
    fake, state = make_fake_newapi(state)
    fake_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://fake-newapi")
    cfg = Settings(database_url=database_url, newapi_base_url="http://fake-newapi", newapi_token="pat-root", jobs_enabled=False, env="test",
                   jwt_secret="test-secret-test-secret-test-secret-0000", public_api_base="https://api.test/v1")
    app = create_app(cfg, newapi_client=fake_client, run_migrations=False, jobs=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield Env(app, client, state, database_url)
    await fake_client.aclose()


class Env:
    def __init__(self, app, client: httpx.AsyncClient, fake: FakeState, url: str):
        self.app, self.client, self.fake, self.url = app, client, fake, url
        self.db = app.state.db
        self.gw = app.state.newapi

    async def register(self, nickname: str, email: str | None = None, role: str = "buyer", password: str = "Passw0rd!x", **extra) -> dict:
        r = await self.client.post("/api/auth/register", json={"nickname": nickname, "email": email or f"{uuid.uuid4().hex[:8]}@test.com", "password": password, "role": role, **extra})
        assert r.status_code == 201, r.text
        return r.json()

    @staticmethod
    def auth(token_or_resp) -> dict:
        tok = token_or_resp["token"] if isinstance(token_or_resp, dict) else token_or_resp
        return {"authorization": f"Bearer {tok}"}

    async def admin(self) -> dict:
        """建一个管理员并返回请求头。"""
        async with self.db.connection() as conn:
            cur = await conn.execute("INSERT INTO users (role, nickname, email, password_hash, credit_score) VALUES ('admin', '管理员', %s, %s, 100) RETURNING id",
                                     (f"admin-{uuid.uuid4().hex[:6]}@test.com", hash_password("AdminPass123")))
            row = await cur.fetchone()
            await conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (row["id"],))
        from app.security import make_jwt
        return self.auth(make_jwt(str(row["id"]), "admin"))

    async def approved_seller(self, nickname: str = "星河算力") -> tuple[dict, dict]:
        """注册 → 申请入驻 → 管理员通过；返回 (注册应答, 请求头)。"""
        reg = await self.register(nickname, role="seller", key_source_note="自有火山引擎企业账号，长期稳定供应 Seedance 与 Doubao")
        h = self.auth(reg)
        r = await self.client.post("/api/seller/apply", json={"key_source_note": "自有火山引擎企业账号，长期稳定供应 Seedance 与 Doubao", "payout_method": {"type": "alipay", "account": "13800000000", "name": "张三"}}, headers=h)
        assert r.status_code == 201, r.text
        adm = await self.admin()
        r = await self.client.post(f"/api/admin/sellers/{reg['user']['id']}/review", json={"approve": True}, headers=adm)
        assert r.status_code == 200, r.text
        return reg, h

    async def funded_buyer(self, nickname: str = "杭州某科技", amount: str = "1000") -> tuple[dict, dict]:
        reg = await self.register(nickname)
        h = self.auth(reg)
        r = await self.client.post("/api/buyer/topups", json={"amount": amount, "pay_method": "alipay"}, headers=h)
        assert r.status_code == 201, r.text
        adm = await self.admin()
        r = await self.client.post(f"/api/admin/topups/{r.json()['id']}/review", json={"approve": True}, headers=adm)
        assert r.status_code == 200, r.text
        return reg, h

    async def listed_key(self, seller_headers: dict, model_id: str = "doubao-seed-1-6", settle: str = "0.60") -> tuple[dict, dict]:
        r = await self.client.post("/api/seller/keys", json={"provider_id": "bytedance_volc", "secret": "sk-good-" + uuid.uuid4().hex, "label": "主账号"}, headers=seller_headers)
        assert r.status_code == 202, r.text
        key = r.json()
        r = await self.client.post("/api/seller/listings", json={"key_id": key["id"], "model_id": model_id, "settle_discount": settle}, headers=seller_headers)
        assert r.status_code == 201, r.text
        return key, r.json()

    async def call_model(self, sk: str, model: str, prompt_tokens: int = 1000, completion_tokens: int = 500) -> httpx.Response:
        """像买方那样用 sk-密钥调用仿引擎。"""
        return await self.gw._client.post("/v1/chat/completions", headers={"authorization": f"Bearer {sk}"},
                                          json={"model": model, "messages": [{"role": "user", "content": "hi"}], "_prompt_tokens": prompt_tokens, "_completion_tokens": completion_tokens})


D = Decimal
