"""New API（v1.0.0-rc.47）客户端：平台和转发引擎之间唯一的通道。

约定（对过真实二进制的应答形状，见算力门 tenant/SPEC.md）：
* 业务错误是 HTTP 200 {success:false, message}；鉴权错误 401/403 带 code；/v1/* 按 OpenAI 风格 {error:{message,code}}。
* 管理操作用 root 的个人访问令牌（PAT）；买方令牌的增删用买方自己的登录会话（平台代管密码）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

MAX_PAGES = 100


class NewApiError(Exception):
    def __init__(self, message: str, status: int = 502, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass
class Credential:
    token: str
    kind: str = "pat"   # pat | session


def _num(v: Any, d: float = 0) -> float:
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def _int(v: Any, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _str(v: Any, d: str = "") -> str:
    return d if v is None else str(v)


class NewApi:
    def __init__(self, base_url: str, root_token: str = "", client: httpx.AsyncClient | None = None, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.root = Credential(root_token, "pat")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owned = client is None

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.base and self.root.token)

    # ------------------------------------------------------------------ 底层
    async def call(self, method: str, path: str, *, cred: Credential | None = None, bearer: str | None = None,
                   body: Any = None, query: dict[str, Any] | None = None) -> dict:
        headers = {"accept": "application/json"}
        token = bearer or (cred.token if cred else None)
        if token:
            headers["authorization"] = f"Bearer {token}"
        params = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
        try:
            r = await self._client.request(method, self.base + path, headers=headers, json=body, params=params)
        except httpx.HTTPError as e:
            raise NewApiError(f"连不上 New API（{self.base}）：{e.__class__.__name__}", 502) from e
        try:
            j = r.json()
        except ValueError:
            j = None
        if not isinstance(j, dict):
            if r.status_code == 429:
                raise NewApiError("New API 请求过于频繁，请稍后再试", 429)
            raise NewApiError(f"New API 返回了无法解析的内容（HTTP {r.status_code}）", r.status_code if r.status_code >= 400 else 502)
        err = j.get("error") if isinstance(j.get("error"), dict) else None
        if err and "success" not in j:
            raise NewApiError(_str(err.get("message")) or f"New API 请求失败（HTTP {r.status_code}）", r.status_code, _str(err.get("code")) or None)
        if r.status_code >= 400 or j.get("success") is False:
            raise NewApiError(_str(j.get("message")) or f"New API 请求失败（HTTP {r.status_code}）", r.status_code, _str(j.get("code")) or None)
        return j

    async def data(self, method: str, path: str, **kw: Any) -> Any:
        return (await self.call(method, path, **kw)).get("data")

    # ------------------------------------------------------------------ 状态
    async def status(self) -> dict:
        d = await self.data("GET", "/api/status") or {}
        qpu = _num(d.get("quota_per_unit"), 500000) or 500000
        return {"version": _str(d.get("version")), "system_name": _str(d.get("system_name")), "quota_per_unit": int(qpu)}

    # ------------------------------------------------------------------ 登录
    async def login(self, username: str, password: str) -> dict:
        """返回 {session: Credential, user: {...}}；账号开了两步验证时抛 NewApiError(code=need_2fa)。"""
        j = await self.call("POST", "/api/user/login", body={"username": username, "password": password})
        d = j.get("data") or {}
        if d.get("require_verification"):
            raise NewApiError("这个账号开了两步验证，平台代管的账号不应开启", 400, "need_2fa")
        token = _str(d.get("access_token"))
        if not token:
            raise NewApiError("New API 登录应答里没有 access_token", 502)
        return {"session": Credential(token, "session"), "user": d.get("user") or {}}

    async def self(self, cred: Credential) -> dict:
        return await self.data("GET", "/api/user/self", cred=cred) or {}

    async def sign_out(self, cred: Credential) -> None:
        """把这次登录开出来的会话注销掉（失败不要紧，会自己过期）。"""
        try:
            sessions = await self.data("GET", "/api/user/sessions", cred=cred) or []
            for s in sessions:
                if isinstance(s, dict) and s.get("current"):
                    await self.call("DELETE", f"/api/user/sessions/{s.get('sid')}", cred=cred)
        except NewApiError:
            pass

    # ------------------------------------------------------------------ 用户（root）
    async def search_user(self, username: str) -> dict | None:
        for p in range(1, MAX_PAGES + 1):
            d = await self.data("GET", "/api/user/search", cred=self.root, query={"keyword": username, "p": p, "page_size": 100}) or {}
            items = d.get("items") or []
            for u in items:
                if u.get("username") == username:
                    return u
            if len(items) < 100 or p * 100 >= _int(d.get("total")):
                return None
        return None

    async def create_user(self, username: str, password: str, display_name: str) -> dict:
        await self.call("POST", "/api/user/", cred=self.root, body={"username": username, "password": password, "display_name": display_name[:20], "role": 1})
        u = await self.search_user(username)
        if not u:
            raise NewApiError(f"用户 {username} 已创建，但读取不到", 502)
        return u

    async def get_user(self, user_id: int) -> dict:
        return await self.data("GET", f"/api/user/{user_id}", cred=self.root) or {}

    async def add_quota(self, user_id: int, quota: int, mode: str = "add") -> None:
        """mode: add / subtract / override。quota 是 New API 额度单位（500000 = 1 美元）。"""
        value = int(quota)
        if mode != "override" and value <= 0:
            return
        await self.call("POST", "/api/user/manage", cred=self.root, body={"id": user_id, "action": "add_quota", "mode": mode, "value": value})

    # ------------------------------------------------------------------ 令牌（买方自己的会话）
    async def list_tokens(self, cred: Credential) -> list[dict]:
        out: list[dict] = []
        for p in range(1, MAX_PAGES + 1):
            d = await self.data("GET", "/api/token/", cred=cred, query={"p": p, "page_size": 100}) or {}
            items = d.get("items") or []
            out.extend(items)
            if len(items) < 100 or len(out) >= _int(d.get("total")):
                break
        return out

    async def create_token(self, cred: Credential, name: str, models: list[str] | None = None, group: str = "") -> dict:
        """返回 {id, key}（key 不带 sk- 前缀，买方用 sk-<key>）。"""
        ms = [m.strip() for m in (models or []) if m and m.strip()]
        await self.call("POST", "/api/token/", cred=cred, body={
            "name": name, "unlimited_quota": True, "expired_time": -1, "remain_quota": 0, "group": group,
            "model_limits_enabled": bool(ms), "model_limits": ",".join(ms), "allow_ips": "",
        })
        found = None
        for t in await self.list_tokens(cred):
            if t.get("name") == name and (found is None or _int(t.get("id")) > _int(found.get("id"))):
                found = t
        if not found:
            raise NewApiError(f"令牌 {name} 已创建，但读取不到", 502)
        key = _str(((await self.data("POST", f"/api/token/{found['id']}/key", cred=cred)) or {}).get("key"))
        if not key:
            raise NewApiError("New API 没有返回令牌密钥", 502)
        return {"id": _int(found.get("id")), "key": key, "raw": found}

    async def update_token(self, cred: Credential, token: dict, *, status: int | None = None, models: list[str] | None = None) -> None:
        body = {k: v for k, v in token.items() if k != "key"}
        if status is not None:
            body["status"] = status
        if models is not None:
            ms = [m.strip() for m in models if m and m.strip()]
            body["model_limits_enabled"] = bool(ms)
            body["model_limits"] = ",".join(ms)
        await self.call("PUT", "/api/token/", cred=cred, body=body)

    async def delete_token(self, cred: Credential, token_id: int) -> None:
        await self.call("DELETE", f"/api/token/{token_id}", cred=cred)

    # ------------------------------------------------------------------ 渠道（root）
    async def fetch_models(self, base_url: str, key: str, channel_type: int = 1) -> list[str]:
        d = await self.data("POST", "/api/channel/fetch_models", cred=self.root, body={"base_url": base_url, "type": channel_type, "key": key})
        return [str(m) for m in d] if isinstance(d, list) else []

    async def list_channels(self) -> list[dict]:
        out: list[dict] = []
        for p in range(1, MAX_PAGES + 1):
            d = await self.data("GET", "/api/channel/", cred=self.root, query={"p": p, "page_size": 100}) or {}
            items = d.get("items") or []
            out.extend(items)
            if len(items) < 100 or len(out) >= _int(d.get("total")):
                break
        return out

    async def create_channel(self, *, name: str, key: str, base_url: str, models: list[str], channel_type: int = 1, group: str = "default",
                             priority: int = 0, weight: int = 0, model_mapping: dict[str, str] | None = None, remark: str = "", status: int = 1) -> dict:
        import json as _json
        channel = {
            "type": channel_type, "name": name, "key": key, "base_url": base_url, "models": ",".join(models), "group": group,
            "priority": priority, "weight": weight, "status": status, "model_mapping": _json.dumps(model_mapping, ensure_ascii=False) if model_mapping else "",
            "auto_ban": 1, "remark": remark,
        }
        await self.call("POST", "/api/channel/", cred=self.root, body={"mode": "single", "channel": channel})
        found = None
        for c in await self.list_channels():
            if c.get("name") == name and (found is None or _int(c.get("id")) > _int(found.get("id"))):
                found = c
        if not found:
            raise NewApiError(f"渠道 {name} 已创建，但读取不到", 502)
        return found

    async def get_channel(self, channel_id: int) -> dict:
        return await self.data("GET", f"/api/channel/{channel_id}", cred=self.root) or {}

    async def patch_channel(self, channel_id: int, **patch: Any) -> None:
        """只传要改的字段（rc.47：空字符串会被忽略，所以空模型列表写不进去）。"""
        await self.call("PUT", "/api/channel/", cred=self.root, body={"id": channel_id, **patch})

    async def set_channel_status(self, channel_id: int, enabled: bool) -> None:
        await self.call("POST", f"/api/channel/{channel_id}/status", cred=self.root, body={"status": 1 if enabled else 2})

    async def delete_channel(self, channel_id: int) -> None:
        await self.call("DELETE", f"/api/channel/{channel_id}", cred=self.root)

    async def test_channel(self, channel_id: int, model: str | None = None) -> dict:
        """走渠道发一个最小请求。测试失败不抛错：{ok: False, message}。"""
        try:
            j = await self.call("GET", f"/api/channel/test/{channel_id}", cred=self.root, query={"model": model})
            return {"ok": True, "message": _str(j.get("message")), "seconds": _num(j.get("time"))}
        except NewApiError as e:
            if e.status >= 500 or e.status in (401, 403):
                raise
            return {"ok": False, "message": e.message, "seconds": 0.0}

    # ------------------------------------------------------------------ 日志（root）
    async def list_logs(self, *, p: int = 1, page_size: int = 100, log_type: int = 2, start: int | None = None, end: int | None = None,
                        channel: int | None = None, username: str | None = None, model: str | None = None) -> dict:
        d = await self.data("GET", "/api/log/", cred=self.root, query={
            "p": p, "page_size": page_size, "type": log_type, "start_timestamp": start, "end_timestamp": end,
            "channel": channel, "username": username, "model_name": model,
        }) or {}
        return {"items": d.get("items") or [], "total": _int(d.get("total")), "page": _int(d.get("page"), p)}

    # ------------------------------------------------------------------ 选项（root）
    async def get_options(self) -> dict[str, str]:
        d = await self.data("GET", "/api/option/", cred=self.root) or []
        return {str(o.get("key")): _str(o.get("value")) for o in d if isinstance(o, dict) and o.get("key") is not None}

    async def set_option(self, key: str, value: str) -> None:
        await self.call("PUT", "/api/option/", cred=self.root, body={"key": key, "value": str(value)})
