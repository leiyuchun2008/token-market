-- =====================================================================
-- Token/API 算力交易平台 · 数据库表结构 v1
-- 数据库：PostgreSQL 15+
-- 约定：
--   * 所有金额用 NUMERIC(18,6)；平台钱包计价货币为 CNY，官方价以 USD 存，按 system_configs.fx_usd_cny 换算
--   * 折扣用小数表示：sell_discount 0.70 = 买方按官方价 7 折付费；settle_discount 0.60 = 卖方按 6 折结算
--   * 所有表都有 is_demo 标记：演示数据 is_demo = true，上线前执行 cleanup_demo.sql 一键清除
--   * 主键用 UUID；时间戳一律 timestamptz
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()

-- ---------- 枚举 ----------
CREATE TYPE user_role        AS ENUM ('buyer', 'seller', 'both', 'admin');
CREATE TYPE user_status      AS ENUM ('active', 'pending_review', 'suspended', 'banned');
CREATE TYPE model_kind       AS ENUM ('text', 'video', 'image', 'audio', 'embedding');
CREATE TYPE billing_mode     AS ENUM ('token', 'video_second', 'image', 'request');
CREATE TYPE key_status       AS ENUM ('validating', 'active', 'paused', 'exhausted', 'dead', 'removed');
CREATE TYPE listing_status   AS ENUM ('active', 'paused', 'removed');
CREATE TYPE txn_type         AS ENUM ('topup', 'consume', 'refund', 'payout', 'deposit_in', 'deposit_out', 'deduction', 'bonus', 'adjust');
CREATE TYPE topup_status     AS ENUM ('pending', 'approved', 'rejected', 'cancelled');
CREATE TYPE payout_status    AS ENUM ('pending', 'approved', 'paid', 'rejected');
CREATE TYPE settle_status    AS ENUM ('open', 'closed', 'payable', 'paid', 'held');
CREATE TYPE usage_status     AS ENUM ('ok', 'upstream_error', 'rate_limited', 'auth_failed', 'timeout', 'rejected');
CREATE TYPE dispute_status   AS ENUM ('open', 'investigating', 'resolved_buyer', 'resolved_seller', 'closed');
CREATE TYPE delivery_status  AS ENUM ('pending_payment', 'paid', 'delivered', 'observing', 'completed', 'refunded', 'disputed');
CREATE TYPE demand_status    AS ENUM ('open', 'matched', 'closed');
CREATE TYPE feed_type        AS ENUM ('seller_joined', 'buyer_joined', 'listing_created', 'topup', 'usage_milestone', 'settlement_paid', 'demand_posted');

-- =====================================================================
-- 1. 用户与身份
-- =====================================================================
CREATE TABLE users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role           user_role    NOT NULL DEFAULT 'buyer',
  status         user_status  NOT NULL DEFAULT 'active',
  nickname       varchar(40)  NOT NULL,              -- 对外只显示昵称，保证买卖双方匿名
  email          varchar(120) UNIQUE,
  phone          varchar(32)  UNIQUE,
  password_hash  text,
  invited_by     uuid REFERENCES users(id),          -- 拉新奖励用
  credit_score   integer      NOT NULL DEFAULT 60,   -- 0-100，买卖双方通用
  is_demo        boolean      NOT NULL DEFAULT false,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  updated_at     timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ON users (role, status);

-- 卖方扩展信息（只有 role in (seller, both) 的用户有一行）
CREATE TABLE seller_profiles (
  user_id              uuid PRIMARY KEY REFERENCES users(id),
  display_name         varchar(60),                  -- 对外显示的店铺名（仍是匿名）
  settlement_cycle_days smallint NOT NULL DEFAULT 7, -- T+N 结算，信用高可缩短
  deposit_required     numeric(18,6) NOT NULL DEFAULT 0,  -- 要求的保证金（CNY）
  deposit_balance      numeric(18,6) NOT NULL DEFAULT 0,  -- 已交保证金余额
  payout_method        jsonb,                        -- 收款方式（银行/支付宝/USDT 等），加密存储
  key_source_note      text,                         -- 入驻时填写的 Key 来源说明
  reviewed_by          uuid REFERENCES users(id),
  reviewed_at          timestamptz,
  is_demo              boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 2. 模型目录与定价
-- =====================================================================
CREATE TABLE providers (                              -- 上游厂商
  id          varchar(40) PRIMARY KEY,                -- 'bytedance_volc', 'aws_bedrock', 'openai', 'anthropic'
  name        varchar(80) NOT NULL,
  base_url    text,                                   -- 默认上游地址，Key 级可覆盖
  api_style   varchar(20) NOT NULL DEFAULT 'openai',  -- 'openai' | 'anthropic' | 'bedrock' | 'volc'
  is_demo     boolean NOT NULL DEFAULT false
);

CREATE TABLE models (
  id                 varchar(80) PRIMARY KEY,          -- 'seedance-1-0-pro'，即对外的 model 参数
  provider_id        varchar(40) NOT NULL REFERENCES providers(id),
  display_name       varchar(120) NOT NULL,
  kind               model_kind   NOT NULL,
  billing_mode       billing_mode NOT NULL,
  -- 官方价（USD）：token 模式用 input/output 每百万 token；video 用每秒；image 用每张；request 用每次
  official_input_usd  numeric(18,6),
  official_output_usd numeric(18,6),
  official_unit_usd   numeric(18,6),
  upstream_model_name varchar(120) NOT NULL,          -- 转发给上游时使用的模型名
  is_featured        boolean NOT NULL DEFAULT false,  -- 首页推荐
  status             varchar(20) NOT NULL DEFAULT 'active',
  is_demo            boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- 平台对买方的售价规则（可按模型、买方等级、时段配置；取最具体的一条）
CREATE TABLE pricing_rules (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id       varchar(80) REFERENCES models(id),   -- NULL = 全局默认
  buyer_tier     smallint,                            -- NULL = 所有等级；1 普通 2 企业 3 VIP
  sell_discount  numeric(6,4) NOT NULL,               -- 0.70 = 官方价 7 折
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to   timestamptz,
  priority       smallint NOT NULL DEFAULT 0,         -- 数字大的优先
  note           text,
  is_demo        boolean NOT NULL DEFAULT false
);
CREATE INDEX ON pricing_rules (model_id, buyer_tier, effective_from);

-- =====================================================================
-- 3. 卖方 Key 托管与挂单
-- =====================================================================
CREATE TABLE provider_keys (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id           uuid NOT NULL REFERENCES users(id),
  provider_id         varchar(40) NOT NULL REFERENCES providers(id),
  label               varchar(80),                     -- 卖方自己给 Key 起的名字
  key_ciphertext      bytea NOT NULL,                  -- 用 KMS/信封加密，网关进程内解密
  key_fingerprint     char(16) NOT NULL,               -- sha256 前 16 位，用于去重（同一 Key 不能重复上传）
  base_url_override   text,
  extra_auth          jsonb,                           -- Bedrock 需要 access/secret/region 等
  status              key_status NOT NULL DEFAULT 'validating',
  daily_cap_usd       numeric(18,6),                   -- 卖方设的每日上限（按官方价计）
  used_today_usd      numeric(18,6) NOT NULL DEFAULT 0,
  consecutive_failures smallint NOT NULL DEFAULT 0,
  success_rate_7d     numeric(5,4),                    -- 探活与真实调用综合
  avg_latency_ms_7d   integer,
  last_probe_at       timestamptz,
  last_used_at        timestamptz,
  dead_at             timestamptz,
  is_demo             boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (seller_id, key_fingerprint)
);
CREATE INDEX ON provider_keys (provider_id, status);

-- 一个 Key 可服务多个模型；挂单 = 某卖方以某结算折扣提供某模型
CREATE TABLE listings (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id        uuid NOT NULL REFERENCES users(id),
  key_id           uuid NOT NULL REFERENCES provider_keys(id),
  model_id         varchar(80) NOT NULL REFERENCES models(id),
  settle_discount  numeric(6,4) NOT NULL,              -- 0.60 = 按官方价 6 折结算给卖方
  weight           smallint NOT NULL DEFAULT 100,      -- 路由权重（后台可调）
  status           listing_status NOT NULL DEFAULT 'active',
  is_demo          boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (key_id, model_id)
);
CREATE INDEX ON listings (model_id, status, settle_discount);

-- Key 探活记录（定时任务写入，保留 30 天）
CREATE TABLE key_probes (
  id          bigserial PRIMARY KEY,
  key_id      uuid NOT NULL REFERENCES provider_keys(id),
  probed_at   timestamptz NOT NULL DEFAULT now(),
  ok          boolean NOT NULL,
  latency_ms  integer,
  error_code  varchar(60),
  is_demo     boolean NOT NULL DEFAULT false
);
CREATE INDEX ON key_probes (key_id, probed_at DESC);

-- =====================================================================
-- 4. 买方接入
-- =====================================================================
CREATE TABLE api_tokens (                              -- 平台签发给买方的子密钥 sk-plat-xxxx
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  buyer_id     uuid NOT NULL REFERENCES users(id),
  name         varchar(60) NOT NULL,
  token_hash   char(64) NOT NULL UNIQUE,               -- 只存 sha256，明文只在创建时返回一次
  token_prefix char(12) NOT NULL,                      -- 'sk-plat-ab12' 用于列表展示
  allowed_models text[],                               -- NULL = 全部
  quota_limit  numeric(18,6),                          -- 该子密钥的消费上限（CNY），NULL = 不限
  quota_used   numeric(18,6) NOT NULL DEFAULT 0,
  rate_limit_rpm integer,
  status       varchar(20) NOT NULL DEFAULT 'active',
  last_used_at timestamptz,
  expires_at   timestamptz,
  is_demo      boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON api_tokens (buyer_id);

-- =====================================================================
-- 5. 钱包与资金流水
-- =====================================================================
CREATE TABLE wallets (
  user_id      uuid PRIMARY KEY REFERENCES users(id),
  balance      numeric(18,6) NOT NULL DEFAULT 0,       -- 可用余额
  frozen       numeric(18,6) NOT NULL DEFAULT 0,       -- 冻结（Key 交付订单付款、进行中的大额请求预留）
  pending_payout numeric(18,6) NOT NULL DEFAULT 0,     -- 卖方：已结算待提现
  currency     char(3) NOT NULL DEFAULT 'CNY',
  version      integer NOT NULL DEFAULT 0,             -- 乐观锁
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE wallet_transactions (                     -- 只追加，不修改
  id            bigserial PRIMARY KEY,
  user_id       uuid NOT NULL REFERENCES users(id),
  type          txn_type NOT NULL,
  amount        numeric(18,6) NOT NULL,                -- 正数入账，负数出账
  balance_after numeric(18,6) NOT NULL,
  ref_type      varchar(30),                           -- 'topup_order' | 'usage_log' | 'settlement' | 'payout' | 'delivery_order'
  ref_id        text,
  note          text,
  is_demo       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON wallet_transactions (user_id, created_at DESC);

CREATE TABLE topup_orders (                            -- V1 人工审核充值
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  buyer_id      uuid NOT NULL REFERENCES users(id),
  amount        numeric(18,6) NOT NULL,
  bonus         numeric(18,6) NOT NULL DEFAULT 0,      -- 首充赠送等
  pay_method    varchar(20) NOT NULL,                  -- 'bank' | 'alipay' | 'wechat' | 'usdt'
  pay_proof_url text,
  status        topup_status NOT NULL DEFAULT 'pending',
  reviewed_by   uuid REFERENCES users(id),
  reviewed_at   timestamptz,
  is_demo       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON topup_orders (status, created_at);

-- =====================================================================
-- 6. 调用计量（核心表，按月分区）
-- =====================================================================
CREATE TABLE usage_logs (
  id               bigserial,
  request_id       uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  buyer_id         uuid NOT NULL,
  api_token_id     uuid NOT NULL,
  model_id         varchar(80) NOT NULL,
  listing_id       uuid,                               -- 实际命中的挂单（失败时可为空）
  key_id           uuid,
  seller_id        uuid,
  -- 用量
  input_tokens     integer,
  output_tokens    integer,
  units            numeric(12,4),                      -- 视频秒数 / 图片张数 / 请求次数
  -- 金额
  official_cost_usd  numeric(18,6) NOT NULL DEFAULT 0,
  fx_rate            numeric(10,4) NOT NULL,
  buyer_cost_cny     numeric(18,6) NOT NULL DEFAULT 0, -- = official × fx × sell_discount
  seller_earning_cny numeric(18,6) NOT NULL DEFAULT 0, -- = official × fx × settle_discount
  platform_margin_cny numeric(18,6) NOT NULL DEFAULT 0,
  -- 结果
  status           usage_status NOT NULL,
  http_status      smallint,
  upstream_error   varchar(120),
  latency_ms       integer,
  retries          smallint NOT NULL DEFAULT 0,        -- 自动切换了几次卖方 Key
  settlement_id    uuid,                               -- 归入哪期结算
  is_demo          boolean NOT NULL DEFAULT false,
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE usage_logs_2026_10 PARTITION OF usage_logs FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE usage_logs_2026_11 PARTITION OF usage_logs FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE usage_logs_2026_12 PARTITION OF usage_logs FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE usage_logs_2027_01 PARTITION OF usage_logs FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');
CREATE TABLE usage_logs_default PARTITION OF usage_logs DEFAULT;   -- 兜底，防止漏建分区时写入失败
-- 运维：每月提前建下月分区；监控 usage_logs_default 行数应始终为 0

CREATE INDEX ON usage_logs (buyer_id, created_at DESC);
CREATE INDEX ON usage_logs (seller_id, created_at DESC);
CREATE INDEX ON usage_logs (key_id, created_at DESC);
CREATE INDEX ON usage_logs (model_id, created_at DESC);
CREATE INDEX ON usage_logs (settlement_id);

-- 按小时预聚合，给首页看板和状态页用，避免直接扫 usage_logs
CREATE TABLE usage_hourly (
  bucket          timestamptz NOT NULL,
  model_id        varchar(80) NOT NULL,
  requests        integer NOT NULL DEFAULT 0,
  ok_requests     integer NOT NULL DEFAULT 0,
  official_cost_usd numeric(18,6) NOT NULL DEFAULT 0,
  buyer_cost_cny  numeric(18,6) NOT NULL DEFAULT 0,
  avg_latency_ms  integer,
  is_demo         boolean NOT NULL DEFAULT false,
  PRIMARY KEY (bucket, model_id)
);

-- =====================================================================
-- 7. 结算、提现、保证金
-- =====================================================================
CREATE TABLE settlements (                             -- 每卖方每周期一行，由定时任务生成
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id      uuid NOT NULL REFERENCES users(id),
  period_start   date NOT NULL,
  period_end     date NOT NULL,
  gross_earning  numeric(18,6) NOT NULL DEFAULT 0,     -- sum(seller_earning_cny)
  deduction      numeric(18,6) NOT NULL DEFAULT 0,     -- Key 失效/纠纷扣款
  fee            numeric(18,6) NOT NULL DEFAULT 0,     -- 提现手续费预提
  payable        numeric(18,6) NOT NULL DEFAULT 0,     -- gross - deduction - fee
  status         settle_status NOT NULL DEFAULT 'open',
  payable_at     timestamptz,                          -- period_end + settlement_cycle_days
  paid_at        timestamptz,
  hold_reason    text,
  is_demo        boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (seller_id, period_start)
);

CREATE TABLE payout_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id     uuid NOT NULL REFERENCES users(id),
  amount        numeric(18,6) NOT NULL,
  fee           numeric(18,6) NOT NULL DEFAULT 0,
  payout_method jsonb NOT NULL,                        -- 快照
  status        payout_status NOT NULL DEFAULT 'pending',
  reviewed_by   uuid REFERENCES users(id),
  paid_at       timestamptz,
  proof_url     text,
  is_demo       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE deductions (                              -- 对卖方的扣款明细，进 settlements.deduction
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id     uuid NOT NULL REFERENCES users(id),
  settlement_id uuid REFERENCES settlements(id),
  reason        varchar(40) NOT NULL,                  -- 'key_dead' | 'dispute' | 'upstream_rollback' | 'manual'
  amount        numeric(18,6) NOT NULL,
  ref_type      varchar(30),
  ref_id        text,
  note          text,
  created_by    uuid REFERENCES users(id),
  is_demo       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 8. 信用与纠纷
-- =====================================================================
CREATE TABLE credit_events (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id),
  event       varchar(40) NOT NULL,                    -- 'key_uptime_week' | 'key_dead' | 'dispute_lost' | 'topup' | 'payout_ontime'
  delta       smallint NOT NULL,
  score_after smallint NOT NULL,
  note        text,
  is_demo     boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON credit_events (user_id, created_at DESC);

CREATE TABLE disputes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  raised_by     uuid NOT NULL REFERENCES users(id),
  against       uuid REFERENCES users(id),             -- 匿名：对方只知道有纠纷，不知道是谁
  ref_type      varchar(30) NOT NULL,                  -- 'usage_log' | 'delivery_order' | 'settlement'
  ref_id        text NOT NULL,
  reason        text NOT NULL,
  evidence      jsonb,
  status        dispute_status NOT NULL DEFAULT 'open',
  resolution    text,
  amount        numeric(18,6),                         -- 判定的赔付/退款金额
  resolved_by   uuid REFERENCES users(id),
  resolved_at   timestamptz,
  is_demo       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 9. V2：Key 交付担保交易、求购单
-- =====================================================================
CREATE TABLE delivery_orders (                         -- 买方要拿原始 Key/账号的担保交易
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  buyer_id        uuid NOT NULL REFERENCES users(id),
  seller_id       uuid NOT NULL REFERENCES users(id),
  provider_id     varchar(40) NOT NULL REFERENCES providers(id),
  title           varchar(120) NOT NULL,               -- '某云 账号 · 含 $5000 额度'
  description     text,
  price_cny       numeric(18,6) NOT NULL,
  observe_days    smallint NOT NULL DEFAULT 3,         -- 观察期
  status          delivery_status NOT NULL DEFAULT 'pending_payment',
  payload_ciphertext bytea,                            -- 卖方交付的 Key/账号，加密，买方确认后解密展示
  validation_result jsonb,                             -- 平台自动验 Key 的结果快照
  delivered_at    timestamptz,
  observe_until   timestamptz,
  completed_at    timestamptz,
  is_demo         boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON delivery_orders (buyer_id, status);
CREATE INDEX ON delivery_orders (seller_id, status);

CREATE TABLE demand_requests (                         -- 求购单
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  buyer_id       uuid NOT NULL REFERENCES users(id),
  model_id       varchar(80) REFERENCES models(id),
  provider_id    varchar(40) REFERENCES providers(id),
  description    text NOT NULL,
  budget_cny     numeric(18,6),
  target_discount numeric(6,4),                        -- 期望的售价折扣
  expected_monthly_usd numeric(18,6),                  -- 预计月用量（官方价）
  status         demand_status NOT NULL DEFAULT 'open',
  matched_listing_id uuid REFERENCES listings(id),
  is_demo        boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 10. 首页动态、系统配置、审计
-- =====================================================================
CREATE TABLE activity_feed (                           -- 首页滚动动态，由业务事件触发写入，已脱敏
  id           bigserial PRIMARY KEY,
  type         feed_type NOT NULL,
  display_text varchar(200) NOT NULL,                  -- '买家 杭**科技 充值了 1 万至 5 万元'
  model_id     varchar(80),
  amount_band  varchar(30),                            -- 只显示区间，不显示精确金额
  is_demo      boolean NOT NULL DEFAULT false,         -- 演示数据在前台带"演示"角标
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON activity_feed (created_at DESC);

CREATE TABLE system_configs (
  key        varchar(60) PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO system_configs (key, value) VALUES
  ('fx_usd_cny',            '7.20'),
  ('default_sell_discount', '0.75'),
  ('topup_fee_rate',        '0'),          -- 充值手续费，初期 0
  ('payout_fee_rate',       '0.01'),
  ('default_settlement_cycle_days', '7'),
  ('key_dead_after_failures', '5'),
  ('probe_interval_minutes', '10'),
  ('reserve_ratio',         '1.2'),        -- 调用前按估算成本 1.2 倍预留余额
  ('first_topup_bonus_rate','0.10');

CREATE TABLE audit_logs (                              -- 后台所有写操作留痕
  id          bigserial PRIMARY KEY,
  actor_id    uuid REFERENCES users(id),
  action      varchar(60) NOT NULL,
  target_type varchar(40),
  target_id   text,
  before      jsonb,
  after       jsonb,
  ip          inet,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 11. 预留：硬件专区（V3 启用，先建表不开放前台）
-- =====================================================================
CREATE TABLE hardware_listings (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seller_id    uuid NOT NULL REFERENCES users(id),
  category     varchar(40) NOT NULL,                   -- 'gpu_server' | 'gpu_cluster' | 'cloud_instance'
  title        varchar(120) NOT NULL,
  spec         jsonb NOT NULL,                         -- 卡型、数量、显存、网络、机房等
  price_cny    numeric(18,6),
  price_unit   varchar(20),                            -- 'per_gpu_hour' | 'per_month' | 'total'
  location     varchar(60),
  status       listing_status NOT NULL DEFAULT 'paused',
  is_demo      boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 12. 常用视图
-- =====================================================================
-- 模型市场页：每个模型的最低售价折扣、在线卖方数、近 7 天可用率
CREATE VIEW v_market_models AS
SELECT m.id, m.display_name, m.kind, m.billing_mode, m.provider_id,
       m.official_input_usd, m.official_output_usd, m.official_unit_usd,
       (SELECT sell_discount FROM pricing_rules p
         WHERE (p.model_id = m.id OR p.model_id IS NULL) AND p.buyer_tier IS NULL
           AND p.effective_from <= now() AND (p.effective_to IS NULL OR p.effective_to > now())
         ORDER BY p.model_id NULLS LAST, p.priority DESC LIMIT 1) AS sell_discount,
       COUNT(DISTINCT l.seller_id) FILTER (WHERE l.status = 'active' AND k.status = 'active') AS online_sellers,
       COALESCE(AVG(k.success_rate_7d) FILTER (WHERE k.status = 'active'), 0) AS availability_7d,
       m.is_featured, m.is_demo
FROM models m
LEFT JOIN listings l ON l.model_id = m.id
LEFT JOIN provider_keys k ON k.id = l.key_id
WHERE m.status = 'active'
GROUP BY m.id;

-- 卖方收益页：按日汇总
CREATE VIEW v_seller_daily_earnings AS
SELECT seller_id, date_trunc('day', created_at) AS day, model_id,
       COUNT(*) AS requests,
       SUM(seller_earning_cny) AS earning_cny,
       SUM(official_cost_usd) AS official_usd
FROM usage_logs
WHERE status = 'ok' AND seller_id IS NOT NULL
GROUP BY seller_id, day, model_id;
