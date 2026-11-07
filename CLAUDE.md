# 给在这个仓库里工作的 Claude

- 用中文回复用户。
- 先读 [README.md](README.md) 和 [docs/progress.md](docs/progress.md)；改数据库先加 `backend/migrations/` 的新文件，不改旧的。
- 分支叫 `主题`（如 `seller-portal`、`settlement-fix`），从最新的 `main` 拉；开草稿 PR，不直接推 `main`；改动和 `docs/progress.md` 的更新放同一个 PR。
- 改完跑 `cd backend && pytest`；测试要一个 PostgreSQL（默认 127.0.0.1:5433，见 tests/conftest.py）。
- 设计到前端页面时，先给用户 3 个方案让他选，选定后再写页面。
- 演示数据一律 `is_demo = true`、昵称「演示·」开头，能一键清除；不要把编造的记录写成真实数据。
- 不提交任何密钥、令牌、`.env`、`data/`、服务器备份文件。
