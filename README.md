# Token/API 算力交易平台

连接 Token / API Key 的供需双方：卖方把 Key 托管给平台，买方预充值后通过平台的 OpenAI 兼容接口调用，平台按量扣费、按周期结算给卖方。
买卖双方互不可见，平台是交易对手方，赚售价与结算价之间的差价。

规划与设计：<https://claude.ai/artifact/7uaBWeKVY6yuSdNifQ3c3m>（产品方案、数据库与接口设计）。接口定义见 [docs/openapi.yaml](docs/openapi.yaml)，运行时文档在 `/api/docs`。

## 架构

```
买方 SDK ──/v1/*──► nginx ──► New API（转发引擎：渠道 = 卖方 Key，用户 = 买方，额度 = 余额）──► 上游厂商
前端/后台 ──/api/*──► nginx ──► 平台后端 FastAPI ──► PostgreSQL（账户、钱包、卖方、挂单、结算、动态）
                                       │
                                       └──同步──► New API（建用户/令牌/渠道、推送售价、拉取调用日志）
```

- **转发引擎**用开源 [New API](https://github.com/QuantumNous/new-api)（AGPL-3.0）：OpenAI 兼容、几十家厂商适配、流式、计费、渠道故障切换都是现成的。平台不改它的代码，只通过它的管理接口驱动。
- **平台后端** `backend/`：Python 3.12 + FastAPI + psycopg3（直接写 SQL，表结构在 `backend/migrations/*.sql`）。
- **前端**：待定（先出 3 个方案选型）。

## 核心流程（已实现）

| 环节 | 做法 |
|---|---|
| 注册 | 买方注册即在引擎里建一个用户（密码由平台加密保管）；想做卖方的提交入驻申请，后台审核 |
| 卖方托管 Key | 引擎用该 Key 去上游列模型，列不出来拒收；通过后建一个渠道（先停用），Key 加密存一份 |
| 挂单 | 卖方选模型、报结算折扣（低于平台售价折扣）；渠道的模型列表 / 名称映射 / 优先级随挂单变化，报价越低优先级越高 |
| 定价 | 后台维护模型目录与官方价、售价折扣；一键推到引擎（ModelRatio / CompletionRatio / ModelPrice） |
| 买方充值 | 线下转账 + 后台审核；通过后钱包入账并给引擎用户加等值额度，首充送 10% |
| 子密钥 | 以买方自己的身份在引擎里建令牌，平台只存哈希和前缀，明文只返回一次 |
| 调用 | 买方直接调 `/v1/chat/completions` 等（nginx 转给引擎），引擎按额度扣费、按渠道优先级路由 |
| 用量同步 | 后台任务按日志 id 游标拉引擎消费日志 → `usage_logs`，扣钱包、算卖方收益与平台毛利 |
| 探活 | 定时对每个 Key 测一次；连续失败到阈值 → 判定失效、挂单暂停、渠道停用、卖方扣信用；恢复后自动复活 |
| 结算 | 每日任务按卖方周期（默认 T+7）生成结算单，到期转成可提现；卖方申请提现，后台打款 |
| 公开接口 | 市场页模型与售价、首页数字、滚动动态（脱敏、`is_demo` 角标）、求购单 |
| 演示数据 | `seed-demo` / `clear-demo` 一键装卸，所有演示行 `is_demo = true` |

## 本地开发

```bash
# PostgreSQL 16（任意方式起一个），然后：
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql://postgres@127.0.0.1:5432/token_market
python -m app.cli migrate
python -m app.cli create-admin admin@example.com 'AdminPass123'
python -m app.cli seed-demo              # 看效果用；clear-demo 清掉
uvicorn app.main:app --reload            # http://127.0.0.1:8000/api/docs
pytest                                   # 测试需要 TEST_PG_ADMIN_URL（默认 postgresql://postgres@127.0.0.1:5433/postgres）
```

没有配置 `NEWAPI_BASE_URL` / `NEWAPI_TOKEN` 时，账户、目录、公开接口、演示数据都能用；涉及引擎的操作（托管 Key、建子密钥、充值到账）会提示「转发引擎还没接入」。

## 服务器部署

```bash
cp .env.example .env    # 填好密码和密钥
docker compose up -d    # postgres、redis、mysql、new-api、api、nginx
# 第一次：
docker compose exec api python -m app.cli create-admin admin@example.com '一个强密码'
# 登录 New API（http://服务器:80 不直接暴露，用 docker compose exec 或临时端口）创建 root 的访问令牌，写进 .env 的 NEWAPI_TOKEN，再 docker compose up -d api
```

`.env`、`data/` 不进仓库。详细进度与待办见 [docs/progress.md](docs/progress.md)。

## 目录

| 目录 | 内容 |
|---|---|
| `backend/app/` | 后端：`routers/`（auth / buyer / seller / admin / public）、`services/`（业务）、`newapi.py`（引擎客户端）、`jobs.py`（后台任务）、`cli.py` |
| `backend/migrations/` | 数据库迁移（0001 = 设计稿的 schema，0002 = 引擎对接字段） |
| `backend/demo/` | 演示数据的装 / 卸 SQL |
| `backend/tests/` | 自动测试（含一个内存里的仿 New API） |
| `deploy/` | nginx 配置、静态页 |
| `docs/` | 接口定义、进度 |
