-- =====================================================================
-- 0002：接入开源 New API 作为转发引擎所需的字段
--   * 买方 = New API 里的一个用户（额度 = 钱包余额），子密钥 = 该用户的令牌
--   * 卖方 Key = New API 里的一个渠道（上游 Key 由平台加密保存一份，网关里一份）
--   * 调用记录由同步任务从 New API 日志拉取，写进 usage_logs
-- =====================================================================

ALTER TABLE users ADD COLUMN newapi_user_id integer UNIQUE;
ALTER TABLE users ADD COLUMN newapi_username varchar(40);
ALTER TABLE users ADD COLUMN newapi_password_ct bytea;          -- 平台代管的 New API 登录密码（加密）

ALTER TABLE providers ADD COLUMN channel_type smallint NOT NULL DEFAULT 1;   -- New API 渠道类型，1 = OpenAI 兼容

ALTER TABLE provider_keys ADD COLUMN key_hint varchar(12) NOT NULL DEFAULT '****';
ALTER TABLE provider_keys ADD COLUMN channel_id integer UNIQUE;
ALTER TABLE provider_keys ADD COLUMN channel_name varchar(60);
ALTER TABLE provider_keys ADD COLUMN upstream_models text[];     -- 校验时上游返回的模型列表

ALTER TABLE api_tokens ADD COLUMN newapi_token_id integer UNIQUE;

ALTER TABLE usage_logs ADD COLUMN newapi_log_id bigint;
CREATE INDEX ON usage_logs (newapi_log_id);

-- 同步游标等运行状态
CREATE TABLE sync_state (
  key        varchar(60) PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO system_configs (key, value) VALUES
  ('newapi_quota_per_unit', '500000'),   -- New API：500000 额度 = 1 美元
  ('min_payout_cny',        '100'),
  ('sync_interval_seconds', '120'),
  ('max_sync_pages',        '200'),
  ('default_channel_group', '"default"')
ON CONFLICT (key) DO NOTHING;
