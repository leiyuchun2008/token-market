"""运行配置：全部来自环境变量（服务器上放在 .env，不进仓库）。"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql://postgres@127.0.0.1:5433/token_market"
    # New API（转发引擎）：容器内地址与 root 的个人访问令牌（PAT，需要 user/channel/log/option 权限）
    newapi_base_url: str = "http://new-api:3000"
    newapi_token: str = ""
    # 买方在文档里看到的接口地址（nginx 把它转给 New API 的 /v1）
    public_api_base: str = "https://api.example.com/v1"
    # JWT 与 Key 加密主密钥（32 字节 hex；泄露即可解出托管的 Key，务必只放服务器）
    jwt_secret: str = Field(default="dev-only-change-me-in-production-00000000")
    jwt_hours: int = 72
    key_encryption_key: str = ""   # 为空时按 jwt_secret 派生（仅开发环境）
    # 后台任务
    jobs_enabled: bool = True
    # 跨域：前端域名，逗号分隔；空 = 不开 CORS
    cors_origins: str = ""

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
