"""统一的业务错误：HTTP 状态 + 机器可读的 code + 给人看的中文 message（接口文档里的 Error 结构）。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .newapi import NewApiError


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


def bad(message: str, field: str | None = None, code: str = "invalid") -> ApiError:
    return ApiError(400, code, message, field)


def not_found(what: str = "记录") -> ApiError:
    return ApiError(404, "not_found", f"{what}不存在")


def forbidden(message: str = "没有权限") -> ApiError:
    return ApiError(403, "forbidden", message)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, e: ApiError):
        body = {"code": e.code, "message": e.message}
        if e.field:
            body["field"] = e.field
        return JSONResponse({"error": body}, status_code=e.status)

    @app.exception_handler(NewApiError)
    async def _newapi(_: Request, e: NewApiError):
        # 转发引擎的问题对买卖双方都是 502：说清楚是网关没应答，不把内部地址露出去
        status = 502 if e.status >= 500 or e.status in (401, 403) else 400
        return JSONResponse({"error": {"code": "gateway", "message": f"转发引擎：{e.message}"}}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        loc = [str(x) for x in first.get("loc", []) if x not in ("body", "query", "path")]
        return JSONResponse({"error": {"code": "invalid", "message": f"参数不对：{'.'.join(loc) or '请求体'}（{first.get('msg', '')}）", "field": ".".join(loc) or None}}, status_code=400)
