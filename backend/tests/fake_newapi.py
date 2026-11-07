"""测试用的仿 New API（内存态）：只实现平台会用到的接口，应答形状照 rc.47。

* 鉴权：Bearer 是 root PAT（"pat-root"）或登录会话；管理接口要求 role >= 10。
* /v1/chat/completions：用 sk-令牌调用，按渠道的模型列表路由，写一条消费日志并扣用户额度（用来模拟买方真实调用）。
* fetch_models：base_url 含 "bad" 或 key 以 "sk-bad" 开头 → 失败；否则返回 state.upstream_models[base_url]（默认一组）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT_PAT = "pat-root"


@dataclass
class FakeState:
    users: list[dict] = field(default_factory=list)
    sessions: dict[str, int] = field(default_factory=dict)        # token -> user_id
    tokens: list[dict] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=lambda: {"QuotaPerUnit": "500000", "ModelRatio": "{}", "CompletionRatio": "{}", "ModelPrice": "{}"})
    upstream_models: dict[str, list[str]] = field(default_factory=dict)
    default_upstream_models: list[str] = field(default_factory=lambda: ["doubao-seedance-1-0-pro", "doubao-seed-1-6", "gpt-4o-mini"])
    channel_test_fail: set[int] = field(default_factory=set)      # 这些渠道 test 返回失败
    calls: list[tuple[str, str]] = field(default_factory=list)
    ids: dict[str, int] = field(default_factory=lambda: {"user": 1, "token": 0, "channel": 0, "log": 0, "sess": 0})
    now: Any = None

    def t(self) -> int:
        return int(self.now()) if self.now else int(time.time())


def ok(data: Any = None, **extra: Any) -> JSONResponse:
    body = {"success": True, "message": "", **extra}
    if data is not None:
        body["data"] = data
    return JSONResponse(body)


def biz(message: str) -> JSONResponse:
    return JSONResponse({"success": False, "message": message})


def denied(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "code": code, "message": message}, status_code=status)


def paginate(items: list[dict], q: dict[str, str]) -> dict:
    p = max(1, int(q.get("p", 1) or 1))
    size = max(1, min(100, int(q.get("page_size", 20) or 20)))
    return {"items": items[(p - 1) * size: p * size], "page": p, "page_size": size, "total": len(items)}


def public_user(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "password"}


def public_token(t: dict) -> dict:
    return {**t, "key": t["key"][:4] + "***"}


def public_channel(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "key"}


def seed_user(state: FakeState, username: str, password: str, role: int = 1, quota: int = 0, display_name: str = "") -> dict:
    u = {"id": state.ids["user"], "username": username, "password": password, "display_name": display_name or username, "role": role,
         "status": 1, "group": "default", "quota": quota, "used_quota": 0, "request_count": 0, "email": "", "remark": ""}
    state.ids["user"] += 1
    state.users.append(u)
    return u


def seed_log(state: FakeState, **fields: Any) -> dict:
    state.ids["log"] += 1
    log = {"id": state.ids["log"], "user_id": 0, "username": "", "token_name": "", "token_id": 0, "model_name": "", "quota": 0,
           "prompt_tokens": 0, "completion_tokens": 0, "use_time": 1, "is_stream": False, "channel": 0, "channel_name": "",
           "created_at": state.t(), "type": 2, "content": "", "group": "default", "ip": "127.0.0.1", "request_id": f"req{state.ids['log']}", "other": "{}"}
    log.update(fields)
    state.logs.append(log)
    return log


def make_fake_newapi(state: FakeState | None = None) -> tuple[FastAPI, FakeState]:
    st = state or FakeState()
    if not st.users:
        seed_user(st, "root", "RootPassw0rd!", role=100)
    app = FakeApp = FastAPI()

    def auth(req: Request) -> dict | None:
        h = req.headers.get("authorization", "")
        if not h.startswith("Bearer "):
            return None
        tok = h[7:].strip()
        if tok == ROOT_PAT:
            return st.users[0]
        uid = st.sessions.get(tok)
        if uid is None:
            return None
        return next((u for u in st.users if u["id"] == uid), None)

    def gate(req: Request, min_role: int = 1):
        u = auth(req)
        if not u:
            return None, denied(401, "AUTH_REQUIRED", "无权进行此操作，未登录且未提供 access token")
        if u["role"] < min_role:
            return None, denied(403, "AUTH_FORBIDDEN", "无权进行此操作，权限不足")
        return u, None

    async def body_of(req: Request) -> dict:
        try:
            b = await req.json()
            return b if isinstance(b, dict) else {}
        except Exception:
            return {}

    @app.middleware("http")
    async def record(req: Request, call_next):
        st.calls.append((req.method, req.url.path))
        return await call_next(req)

    # ---- 状态 / 登录 ----
    @app.get("/api/status")
    async def status():
        return ok({"version": "v1.0.0-rc.47", "system_name": "fake", "quota_per_unit": int(st.options["QuotaPerUnit"]), "display_in_currency": True})

    @app.post("/api/user/login")
    async def login(req: Request):
        b = await body_of(req)
        u = next((x for x in st.users if x["username"] == b.get("username")), None)
        if not u or u["password"] != b.get("password"):
            return biz("用户名或密码错误")
        if u["status"] != 1:
            return biz("用户已被封禁")
        st.ids["sess"] += 1
        tok = f"sess-{st.ids['sess']}-{u['id']}"
        st.sessions[tok] = u["id"]
        return ok({"access_token": tok, "access_expires_at": st.t() + 3600, "user": public_user(u)})

    @app.get("/api/user/self")
    async def self_(req: Request):
        u, err = gate(req)
        return err or ok(public_user(u))

    @app.get("/api/user/sessions")
    async def sessions(req: Request):
        u, err = gate(req)
        if err:
            return err
        cur = req.headers["authorization"][7:]
        return ok([{"sid": t, "current": t == cur} for t, uid in st.sessions.items() if uid == u["id"]])

    @app.delete("/api/user/sessions/{sid}")
    async def revoke(req: Request, sid: str):
        u, err = gate(req)
        if err:
            return err
        st.sessions.pop(sid, None)
        return ok()

    # ---- 用户（管理员）----
    @app.get("/api/user/search")
    async def search(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        kw = (req.query_params.get("keyword") or "").lower()
        items = [public_user(x) for x in sorted(st.users, key=lambda x: -x["id"]) if not kw or kw in x["username"].lower()]
        return ok(paginate(items, dict(req.query_params)))

    @app.post("/api/user/")
    async def create_user(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        if not b.get("username"):
            return biz("Invalid parameters")
        if len(str(b.get("password", ""))) < 8:
            return biz("Password must contain between 8 and 128 characters.")
        if any(x["username"] == b["username"] for x in st.users):
            return biz("constraint failed: UNIQUE constraint failed: users.username (2067)")
        seed_user(st, b["username"], b["password"], role=int(b.get("role") or 1), display_name=str(b.get("display_name") or ""))
        return ok()

    @app.get("/api/user/{uid}")
    async def get_user(req: Request, uid: int):
        u, err = gate(req, 10)
        if err:
            return err
        x = next((y for y in st.users if y["id"] == uid), None)
        return ok(public_user(x)) if x else biz("record not found")

    @app.post("/api/user/manage")
    async def manage(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        x = next((y for y in st.users if y["id"] == int(b.get("id") or 0)), None)
        if not x:
            return biz("User does not exist")
        if b.get("action") == "add_quota":
            v, mode = b.get("value"), b.get("mode")
            if not isinstance(v, int) or mode not in ("add", "subtract", "override"):
                return biz("Invalid parameters")
            x["quota"] = x["quota"] + v if mode == "add" else x["quota"] - v if mode == "subtract" else v
            seed_log(st, user_id=x["id"], username=x["username"], type=1, content=f"quota {mode} {v}")
            return ok()
        return biz("Invalid parameters")

    # ---- 令牌（自己的）----
    @app.get("/api/token/")
    async def list_tokens(req: Request):
        u, err = gate(req)
        if err:
            return err
        items = [public_token(t) for t in sorted(st.tokens, key=lambda t: -t["id"]) if t["user_id"] == u["id"]]
        return ok(paginate(items, dict(req.query_params)))

    @app.post("/api/token/")
    async def create_token(req: Request):
        u, err = gate(req)
        if err:
            return err
        b = await body_of(req)
        st.ids["token"] += 1
        import secrets
        t = {"id": st.ids["token"], "user_id": u["id"], "name": str(b.get("name") or ""), "key": secrets.token_hex(24), "status": 1,
             "created_time": st.t(), "accessed_time": st.t(), "expired_time": -1, "remain_quota": 0, "unlimited_quota": bool(b.get("unlimited_quota")),
             "used_quota": 0, "group": str(b.get("group") or ""), "model_limits_enabled": bool(b.get("model_limits_enabled")),
             "model_limits": str(b.get("model_limits") or ""), "allow_ips": ""}
        st.tokens.append(t)
        return ok()

    @app.put("/api/token/")
    async def update_token(req: Request):
        u, err = gate(req)
        if err:
            return err
        b = await body_of(req)
        t = next((x for x in st.tokens if x["id"] == int(b.get("id") or 0) and x["user_id"] == u["id"]), None)
        if not t:
            return biz("record not found")
        for k in ("name", "group", "model_limits_enabled", "model_limits", "status", "unlimited_quota"):
            if k in b:
                t[k] = b[k]
        return ok(public_token(t))

    @app.post("/api/token/{tid}/key")
    async def reveal(req: Request, tid: int):
        u, err = gate(req)
        if err:
            return err
        t = next((x for x in st.tokens if x["id"] == tid and x["user_id"] == u["id"]), None)
        return ok({"key": t["key"]}) if t else biz("record not found")

    @app.delete("/api/token/{tid}")
    async def delete_token(req: Request, tid: int):
        u, err = gate(req)
        if err:
            return err
        before = len(st.tokens)
        st.tokens = [x for x in st.tokens if not (x["id"] == tid and x["user_id"] == u["id"])]
        return ok() if len(st.tokens) < before else biz("record not found")

    # ---- 渠道（管理员）----
    @app.post("/api/channel/fetch_models")
    async def fetch_models(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        base, key = str(b.get("base_url") or ""), str(b.get("key") or "")
        if not base:
            return biz("Failed to fetch models: Forbidden")
        if "bad" in base or key.startswith("sk-bad"):
            return biz("Failed to fetch models: status code: 401")
        return ok(st.upstream_models.get(base, st.default_upstream_models))

    @app.get("/api/channel/")
    async def list_channels(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        return ok(paginate([public_channel(c) for c in sorted(st.channels, key=lambda c: -c["id"])], dict(req.query_params)))

    @app.post("/api/channel/")
    async def create_channel(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        c = b.get("channel") if isinstance(b.get("channel"), dict) else None
        if not c or not c.get("key"):
            return biz("channel cannot be empty")
        st.ids["channel"] += 1
        st.channels.append({"id": st.ids["channel"], "type": int(c.get("type") or 1), "name": str(c.get("name") or ""), "key": str(c["key"]),
                            "base_url": str(c.get("base_url") or ""), "models": str(c.get("models") or ""), "group": str(c.get("group") or "default"),
                            "status": int(c.get("status") if c.get("status") is not None else 1), "priority": int(c.get("priority") or 0),
                            "weight": int(c.get("weight") or 0), "model_mapping": str(c.get("model_mapping") or ""), "remark": str(c.get("remark") or ""),
                            "used_quota": 0, "response_time": 0, "test_time": 0})
        return ok()

    @app.put("/api/channel/")
    async def patch_channel(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        if "status" in b:
            return biz("Invalid parameters")
        c = next((x for x in st.channels if x["id"] == int(b.get("id") or 0)), None)
        if not c:
            return biz("record not found")
        for k in ("name", "base_url", "models", "group", "key"):
            if b.get(k):
                c[k] = str(b[k])
        for k in ("priority", "weight"):
            if isinstance(b.get(k), int):
                c[k] = b[k]
        for k in ("model_mapping", "remark"):
            if k in b:
                c[k] = str(b[k] or "")
        return ok(public_channel(c))

    @app.post("/api/channel/{cid}/status")
    async def channel_status(req: Request, cid: int):
        u, err = gate(req, 10)
        if err:
            return err
        b = await body_of(req)
        if b.get("status") not in (1, 2):
            return biz("Invalid parameters")
        c = next((x for x in st.channels if x["id"] == cid), None)
        if not c:
            return ok(False)
        c["status"] = b["status"]
        return ok(True)

    @app.get("/api/channel/test/{cid}")
    async def test_channel(req: Request, cid: int):
        u, err = gate(req, 10)
        if err:
            return err
        c = next((x for x in st.channels if x["id"] == cid), None)
        if not c:
            return biz("record not found")
        if cid in st.channel_test_fail:
            return JSONResponse({"success": False, "message": "status code 401: invalid api key", "time": 0.01})
        c["response_time"] = 12
        c["test_time"] = st.t()
        return JSONResponse({"success": True, "message": "", "time": 0.012})

    @app.get("/api/channel/{cid}")
    async def get_channel(req: Request, cid: int):
        u, err = gate(req, 10)
        if err:
            return err
        c = next((x for x in st.channels if x["id"] == cid), None)
        return ok(public_channel(c)) if c else biz("record not found")

    @app.delete("/api/channel/{cid}")
    async def delete_channel(req: Request, cid: int):
        u, err = gate(req, 10)
        if err:
            return err
        st.channels = [x for x in st.channels if x["id"] != cid]
        return ok()

    # ---- 选项（root）----
    @app.get("/api/option/")
    async def get_options(req: Request):
        u, err = gate(req, 100)
        if err:
            return err
        return ok([{"key": k, "value": v} for k, v in st.options.items()])

    @app.put("/api/option/")
    async def set_option(req: Request):
        u, err = gate(req, 100)
        if err:
            return err
        b = await body_of(req)
        st.options[str(b.get("key"))] = str(b.get("value"))
        return ok()

    # ---- 日志（管理员）----
    @app.get("/api/log/")
    async def list_logs(req: Request):
        u, err = gate(req, 10)
        if err:
            return err
        q = dict(req.query_params)
        typ = int(q.get("type") or 0)
        start, end = int(q.get("start_timestamp") or 0), int(q.get("end_timestamp") or 0)
        channel = int(q.get("channel") or 0)
        username, model = q.get("username") or "", q.get("model_name") or ""
        items = [l for l in st.logs if (not typ or l["type"] == typ) and (not start or l["created_at"] >= start) and (not end or l["created_at"] <= end)
                 and (not channel or l["channel"] == channel) and (not username or l["username"] == username) and (not model or l["model_name"] == model)]
        items.sort(key=lambda l: (-l["created_at"], -l["id"]))
        return ok(paginate(items, q))

    # ---- 转发：买方用 sk-令牌调用 ----
    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        h = req.headers.get("authorization", "")
        key = h[7:].strip() if h.startswith("Bearer ") else ""
        key = key[3:] if key.startswith("sk-") else key
        tok = next((t for t in st.tokens if t["key"] == key), None)
        if not tok or tok["status"] != 1:
            return JSONResponse({"error": {"message": "invalid api key", "type": "new_api_error", "code": "invalid_api_key"}}, status_code=401)
        user = next((x for x in st.users if x["id"] == tok["user_id"]), None)
        b = await body_of(req)
        model = str(b.get("model") or "")
        if tok["model_limits_enabled"] and model not in [m.strip() for m in tok["model_limits"].split(",")]:
            return JSONResponse({"error": {"message": f"token does not allow model {model}", "type": "new_api_error", "code": "model_not_allowed"}}, status_code=403)
        chans = sorted([c for c in st.channels if c["status"] == 1 and model in [m.strip() for m in c["models"].split(",")]], key=lambda c: -c["priority"])
        if not chans:
            return JSONResponse({"error": {"message": f"no available channel for model {model}", "type": "new_api_error", "code": "no_channel"}}, status_code=503)
        ratios = json.loads(st.options.get("ModelRatio") or "{}")
        prices = json.loads(st.options.get("ModelPrice") or "{}")
        comp = json.loads(st.options.get("CompletionRatio") or "{}")
        if model not in ratios and model not in prices:
            return JSONResponse({"error": {"message": f"model {model} price not configured", "type": "new_api_error", "code": "model_price_error"}}, status_code=400)
        prompt_tokens = int(b.get("_prompt_tokens") or 12)
        completion_tokens = int(b.get("_completion_tokens") or 7)
        qpu = int(st.options["QuotaPerUnit"])
        if model in prices:
            quota = int(round(prices[model] * qpu))          # 按次计价：价格（美元）× 每美元额度
        else:
            # New API 按 token 计价：quota = ratio × (prompt + completion × completion_ratio)（qpu=500000 时 1 ratio = $2 / 百万 token）
            quota = int(round(ratios[model] * (prompt_tokens + completion_tokens * comp.get(model, 1.0))))
        if user["quota"] < quota:
            return JSONResponse({"error": {"message": "insufficient user quota", "type": "new_api_error", "code": "insufficient_user_quota"}}, status_code=403)
        ch = chans[0]
        user["quota"] -= quota
        user["used_quota"] += quota
        user["request_count"] += 1
        tok["used_quota"] += quota
        seed_log(st, user_id=user["id"], username=user["username"], token_name=tok["name"], token_id=tok["id"], model_name=model, quota=quota,
                 prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, channel=ch["id"], channel_name=ch["name"], content="fake")
        return JSONResponse({"id": "chatcmpl-fake", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}})

    return app, st
