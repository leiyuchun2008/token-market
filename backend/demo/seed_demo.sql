-- =====================================================================
-- 演示数据（仅用于开发联调和看前台效果）
-- * 所有行 is_demo = true；昵称以 "演示·" 开头；邮箱域名 example.com
-- * 清除方式：执行 cleanup_demo.sql
-- * 前台约定：is_demo = true 的记录显示"演示"角标；/api/public/stats 返回 includes_demo = true
-- 依赖：先执行 schema.sql；需要 pgcrypto
-- =====================================================================

BEGIN;

-- 固定随机种子，便于每次生成一致
SELECT setseed(0.42);

-- ---------- 上游厂商 ----------
INSERT INTO providers (id, name, base_url, api_style, is_demo) VALUES
  ('bytedance_volc', '火山引擎（字节跳动）', 'https://ark.cn-beijing.volces.com/api/v3', 'volc',     true),
  ('aws_bedrock',    'AWS Bedrock',            NULL,                                     'bedrock',  true),
  ('openai',         'OpenAI',                 'https://api.openai.com/v1',              'openai',   true),
  ('anthropic',      'Anthropic',              'https://api.anthropic.com',              'anthropic',true),
  ('google',         'Google Gemini',          'https://generativelanguage.googleapis.com', 'openai', true)
ON CONFLICT (id) DO NOTHING;

-- ---------- 模型目录（官方价为演示用的大致数字，上线前按官网核对） ----------
INSERT INTO models (id, provider_id, display_name, kind, billing_mode,
                    official_input_usd, official_output_usd, official_unit_usd, upstream_model_name, is_featured, is_demo) VALUES
  ('seedance-1-0-pro',   'bytedance_volc', 'Seedance 1.0 Pro（视频）',  'video', 'video_second', NULL, NULL, 0.08,  'doubao-seedance-1-0-pro',  true,  true),
  ('seedance-1-0-lite',  'bytedance_volc', 'Seedance 1.0 Lite（视频）', 'video', 'video_second', NULL, NULL, 0.04,  'doubao-seedance-1-0-lite', true,  true),
  ('seedream-4',         'bytedance_volc', 'Seedream 4.0（图片）',      'image', 'image',        NULL, NULL, 0.03,  'doubao-seedream-4-0',      false, true),
  ('doubao-seed-1-6',    'bytedance_volc', 'Doubao Seed 1.6（文本）',   'text',  'token',        0.11, 1.10, NULL,  'doubao-seed-1-6',          false, true),
  ('claude-sonnet-4-5',  'aws_bedrock',    'Claude Sonnet 4.5（Bedrock）','text','token',        3.00, 15.00, NULL, 'anthropic.claude-sonnet-4-5', true, true),
  ('claude-haiku-4-5',   'aws_bedrock',    'Claude Haiku 4.5（Bedrock）','text', 'token',        1.00, 5.00, NULL,  'anthropic.claude-haiku-4-5', false, true),
  ('gpt-4-1',            'openai',         'GPT-4.1',                   'text',  'token',        2.00, 8.00, NULL,  'gpt-4.1',                  false, true),
  ('gpt-4-1-mini',       'openai',         'GPT-4.1 mini',              'text',  'token',        0.40, 1.60, NULL,  'gpt-4.1-mini',             false, true),
  ('gemini-2-5-pro',     'google',         'Gemini 2.5 Pro',            'text',  'token',        1.25, 10.00, NULL, 'gemini-2.5-pro',           false, true),
  ('text-embedding-3-large','openai',      'text-embedding-3-large',    'embedding','token',     0.13, NULL, NULL,  'text-embedding-3-large',   false, true)
ON CONFLICT (id) DO NOTHING;

-- ---------- 售价规则：全局 7.5 折，Seedance 系列 8 折（稀缺），企业级 7 折 ----------
INSERT INTO pricing_rules (model_id, buyer_tier, sell_discount, priority, note, is_demo) VALUES
  (NULL,               NULL, 0.75, 0,  '全局默认', true),
  ('seedance-1-0-pro', NULL, 0.80, 10, '稀缺模型加价', true),
  ('seedance-1-0-lite',NULL, 0.80, 10, '稀缺模型加价', true),
  (NULL,               2,    0.70, 5,  '企业买方', true);

-- ---------- 管理员 ----------
INSERT INTO users (id, role, nickname, email, password_hash, credit_score, is_demo) VALUES
  ('00000000-0000-0000-0000-000000000001', 'admin', '演示·管理员', 'demo-admin@example.com', crypt('demo-admin-123', gen_salt('bf')), 100, true);

-- ---------- 12 个卖方 ----------
INSERT INTO users (id, role, status, nickname, email, password_hash, credit_score, is_demo, created_at)
SELECT ('10000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       'seller', 'active',
       '演示·' || (ARRAY['云海','星河','北辰','青木','墨阳','晨光','远航','临风','松涛','知远','云途','澜海'])[i] || '算力',
       'seller' || i || '@example.com',
       crypt('demo-seller-123', gen_salt('bf')),
       55 + (random() * 40)::int,
       true,
       now() - ((random() * 45) * interval '1 days')
FROM generate_series(1, 12) i;

INSERT INTO seller_profiles (user_id, display_name, settlement_cycle_days, deposit_required, deposit_balance, key_source_note, reviewed_by, reviewed_at, is_demo)
SELECT u.id, u.nickname,
       CASE WHEN u.credit_score >= 85 THEN 3 WHEN u.credit_score >= 70 THEN 7 ELSE 14 END,
       5000, 5000,
       '演示数据：企业账号闲置额度',
       '00000000-0000-0000-0000-000000000001', u.created_at + interval '1 day', true
FROM users u WHERE u.role = 'seller' AND u.is_demo;

-- ---------- 40 个买方 ----------
INSERT INTO users (id, role, status, nickname, email, password_hash, credit_score, is_demo, created_at)
SELECT ('20000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       'buyer', 'active',
       '演示·' || (ARRAY['杭','深','沪','京','蓉','穗','宁','汉','苏','厦'])[1 + (i % 10)] || '**'
                || (ARRAY['科技','智能','传媒','影业','数科','网络','创想','互娱'])[1 + (i % 8)],
       'buyer' || i || '@example.com',
       crypt('demo-buyer-123', gen_salt('bf')),
       50 + (random() * 45)::int,
       true,
       now() - ((random() * 40) * interval '1 days')
FROM generate_series(1, 40) i;

-- 钱包：所有演示用户一人一行
INSERT INTO wallets (user_id, balance, frozen, pending_payout)
SELECT id,
       CASE WHEN role = 'buyer' THEN round((500 + random() * 29500)::numeric, 2) ELSE 0 END,
       0,
       CASE WHEN role = 'seller' THEN round((random() * 8000)::numeric, 2) ELSE 0 END
FROM users WHERE is_demo;

-- ---------- 托管 Key：每个卖方 1 到 3 个 ----------
INSERT INTO provider_keys (id, seller_id, provider_id, label, key_ciphertext, key_fingerprint, status,
                           daily_cap_usd, used_today_usd, success_rate_7d, avg_latency_ms_7d, last_probe_at, is_demo, created_at)
SELECT gen_random_uuid(), s.id,
       (ARRAY['bytedance_volc','bytedance_volc','aws_bedrock','openai','anthropic','google'])[1 + (random() * 5)::int],
       '演示Key-' || n,
       '\x44454d4f'::bytea,                                   -- 'DEMO'，不是真实密文
       substr(md5(s.id::text || n), 1, 16),
       (ARRAY['active','active','active','active','paused','exhausted'])[1 + (random() * 5)::int]::key_status,
       (ARRAY[200, 500, 1000, 2000, NULL])[1 + (random() * 4)::int],
       round((random() * 150)::numeric, 2),
       round((0.93 + random() * 0.07)::numeric, 4),
       400 + (random() * 2500)::int,
       now() - ((random() * 10) * interval '1 minutes'),
       true,
       s.created_at + interval '1 day'
FROM users s
CROSS JOIN LATERAL generate_series(1, 1 + (random() * 2)::int) n
WHERE s.role = 'seller' AND s.is_demo;

-- ---------- 挂单：每个 Key 按其厂商挂 1 到 3 个模型 ----------
INSERT INTO listings (seller_id, key_id, model_id, settle_discount, status, is_demo, created_at)
SELECT DISTINCT ON (k.id, m.id)
       k.seller_id, k.id, m.id,
       round((0.50 + random() * 0.18)::numeric, 2),         -- 卖方结算 5 到 6.8 折
       CASE WHEN k.status = 'active' THEN 'active' ELSE 'paused' END::listing_status,
       true, k.created_at
FROM provider_keys k
JOIN models m ON m.provider_id = k.provider_id AND m.is_demo
WHERE k.is_demo AND random() < 0.7;

-- ---------- 买方子密钥：每个买方 1 到 2 个 ----------
INSERT INTO api_tokens (buyer_id, name, token_hash, token_prefix, quota_limit, quota_used, status, is_demo, created_at)
SELECT b.id,
       (ARRAY['生产环境','测试环境','视频产线','客服机器人'])[n],
       encode(sha256((b.id::text || n)::bytea), 'hex'),
       'sk-plat-' || substr(md5(b.id::text || n), 1, 4),
       (ARRAY[NULL, 5000, 20000])[1 + (random() * 2)::int],
       0, 'active', true, b.created_at + interval '2 hours'
FROM users b
CROSS JOIN LATERAL generate_series(1, 1 + (random() * 1)::int) n
WHERE b.role = 'buyer' AND b.is_demo;

-- ---------- 充值记录：每个买方 1 到 4 笔已审核 ----------
INSERT INTO topup_orders (buyer_id, amount, bonus, pay_method, status, reviewed_by, reviewed_at, is_demo, created_at)
SELECT b.id,
       (ARRAY[1000, 2000, 5000, 10000, 20000, 50000])[1 + (random() * 5)::int],
       0,
       (ARRAY['bank','alipay','wechat','usdt'])[1 + (random() * 3)::int],
       'approved',
       '00000000-0000-0000-0000-000000000001',
       LEAST(b.created_at + ((n) * interval '1 days') + interval '3 hours', now() - interval '1 hour'),
       true,
       LEAST(b.created_at + ((n) * interval '1 days'), now() - interval '4 hours')
FROM users b
CROSS JOIN LATERAL generate_series(1, 1 + (random() * 3)::int) n
WHERE b.role = 'buyer' AND b.is_demo;

-- 对应的钱包流水
INSERT INTO wallet_transactions (user_id, type, amount, balance_after, ref_type, ref_id, is_demo, created_at)
SELECT buyer_id, 'topup', amount, amount, 'topup_order', id::text, true, reviewed_at
FROM topup_orders WHERE is_demo;

-- ---------- 调用记录：近 30 天约 20 万条（只写当月和上月分区，按需调整数量） ----------
-- 实测 PostgreSQL 16 约 9 秒；想少一点把 200000 改小
INSERT INTO usage_logs (request_id, created_at, buyer_id, api_token_id, model_id, listing_id, key_id, seller_id,
                        input_tokens, output_tokens, units, official_cost_usd, fx_rate,
                        buyer_cost_cny, seller_earning_cny, platform_margin_cny,
                        status, http_status, latency_ms, retries, is_demo)
SELECT gen_random_uuid(),
       ts,
       t.buyer_id, t.id, l.model_id, l.id, l.key_id, l.seller_id,
       CASE WHEN m.billing_mode = 'token' THEN (200 + random() * 6000)::int END,
       CASE WHEN m.billing_mode = 'token' THEN (50 + random() * 2500)::int END,
       CASE WHEN m.billing_mode = 'video_second' THEN (ARRAY[5, 10])[1 + (random())::int]
            WHEN m.billing_mode = 'image' THEN 1 + (random() * 3)::int END,
       cost.official_usd, 7.20,
       round(cost.official_usd * 7.20 * sd.sell_discount, 6),
       round(cost.official_usd * 7.20 * l.settle_discount, 6),
       round(cost.official_usd * 7.20 * (sd.sell_discount - l.settle_discount), 6),
       (CASE WHEN random() < 0.965 THEN 'ok' WHEN random() < 0.5 THEN 'rate_limited' ELSE 'upstream_error' END)::usage_status,
       200,
       CASE WHEN m.billing_mode = 'video_second' THEN 20000 + (random() * 60000)::int ELSE 300 + (random() * 4000)::int END,
       (CASE WHEN random() < 0.06 THEN 1 ELSE 0 END),
       true
FROM generate_series(1, 200000) g
CROSS JOIN LATERAL (SELECT now() - ((random() * 30 + g * 0) * interval '1 days') AS ts) tt   -- g * 0 让子查询逐行求值
CROSS JOIN LATERAL (SELECT id, buyer_id FROM api_tokens WHERE is_demo ORDER BY random() + g * 0 LIMIT 1) t
CROSS JOIN LATERAL (SELECT l.* FROM listings l WHERE l.is_demo AND l.status = 'active' ORDER BY random() + g * 0 LIMIT 1) l
JOIN models m ON m.id = l.model_id
CROSS JOIN LATERAL (
  SELECT CASE m.billing_mode
           WHEN 'token'        THEN round((((random() * 6000)::numeric * COALESCE(m.official_input_usd, 0) + (random() * 2500)::numeric * COALESCE(m.official_output_usd, 0)) / 1e6)::numeric, 6)
           WHEN 'video_second' THEN round(((5 + (random())::int * 5)::numeric * m.official_unit_usd)::numeric, 6)
           WHEN 'image'        THEN round(((1 + (random() * 3)::int)::numeric * m.official_unit_usd)::numeric, 6)
           ELSE 0.001 END AS official_usd) cost
CROSS JOIN LATERAL (
  SELECT sell_discount FROM pricing_rules p
  WHERE (p.model_id = m.id OR p.model_id IS NULL) AND p.buyer_tier IS NULL
  ORDER BY p.model_id NULLS LAST, p.priority DESC LIMIT 1) sd;

-- 失败的调用不计费
UPDATE usage_logs SET buyer_cost_cny = 0, seller_earning_cny = 0, platform_margin_cny = 0, http_status = 502
WHERE is_demo AND status <> 'ok';

-- 小时聚合
INSERT INTO usage_hourly (bucket, model_id, requests, ok_requests, official_cost_usd, buyer_cost_cny, avg_latency_ms, is_demo)
SELECT date_trunc('hour', created_at), model_id, COUNT(*), COUNT(*) FILTER (WHERE status = 'ok'),
       SUM(official_cost_usd), SUM(buyer_cost_cny), AVG(latency_ms)::int, true
FROM usage_logs WHERE is_demo
GROUP BY 1, 2
ON CONFLICT (bucket, model_id) DO UPDATE SET requests = EXCLUDED.requests, ok_requests = EXCLUDED.ok_requests,
  official_cost_usd = EXCLUDED.official_cost_usd, buyer_cost_cny = EXCLUDED.buyer_cost_cny, avg_latency_ms = EXCLUDED.avg_latency_ms;

-- ---------- 结算单：每个卖方过去 4 周每周一张 ----------
INSERT INTO settlements (seller_id, period_start, period_end, gross_earning, deduction, fee, payable, status, payable_at, paid_at, is_demo)
SELECT s.id,
       (current_date - (7 * w + 6))::date,
       (current_date - (7 * w))::date,
       COALESCE(e.earning, 0), 0, round(COALESCE(e.earning, 0) * 0.01, 2), round(COALESCE(e.earning, 0) * 0.99, 2),
       CASE WHEN w >= 2 THEN 'paid' WHEN w = 1 THEN 'payable' ELSE 'closed' END::settle_status,
       (current_date - (7 * w)) + interval '7 days',
       CASE WHEN w >= 2 THEN (current_date - (7 * w)) + interval '8 days' END,
       true
FROM users s
CROSS JOIN generate_series(0, 3) w
LEFT JOIN LATERAL (
  SELECT SUM(seller_earning_cny) AS earning FROM usage_logs u
  WHERE u.seller_id = s.id AND u.status = 'ok'
    AND u.created_at >= (current_date - (7 * w + 6)) AND u.created_at < (current_date - (7 * w)) + 1) e ON true
WHERE s.role = 'seller' AND s.is_demo;

-- 提现记录：已付结算对应提现
INSERT INTO payout_requests (seller_id, amount, fee, payout_method, status, reviewed_by, paid_at, is_demo, created_at)
SELECT seller_id, payable, fee, '{"type":"bank","masked":"**** 6789"}'::jsonb, 'paid',
       '00000000-0000-0000-0000-000000000001', paid_at, true, paid_at - interval '1 day'
FROM settlements WHERE is_demo AND status = 'paid' AND payable > 0;

-- ---------- 信用事件 ----------
INSERT INTO credit_events (user_id, event, delta, score_after, note, is_demo, created_at)
SELECT id, 'key_uptime_week', 2, credit_score, '演示：Key 本周可用率 > 99%', true, now() - interval '3 days'
FROM users WHERE role = 'seller' AND is_demo;

-- ---------- 求购单（V2 预览） ----------
INSERT INTO demand_requests (buyer_id, model_id, description, budget_cny, target_discount, expected_monthly_usd, status, is_demo, created_at)
SELECT b.id, (ARRAY['seedance-1-0-pro','claude-sonnet-4-5','gpt-4-1'])[1 + (random() * 2)::int],
       '演示：长期稳定供应，月用量见预估', 20000 + (random() * 80000)::int, 0.7, 5000 + (random() * 20000)::int, 'open', true,
       now() - ((random() * 5) * interval '1 days')
FROM users b WHERE b.role = 'buyer' AND b.is_demo ORDER BY random() LIMIT 6;

-- ---------- 首页动态（脱敏文案，由上面的事件生成） ----------
INSERT INTO activity_feed (type, display_text, model_id, amount_band, is_demo, created_at)
SELECT 'seller_joined'::feed_type, nickname || ' 完成入驻，托管了 ' || (SELECT COUNT(*) FROM provider_keys k WHERE k.seller_id = u.id) || ' 个 Key', NULL, NULL, true, created_at
FROM users u WHERE role = 'seller' AND is_demo
UNION ALL
SELECT 'buyer_joined'::feed_type, nickname || ' 注册成为买家', NULL, NULL, true, created_at
FROM users WHERE role = 'buyer' AND is_demo
UNION ALL
SELECT 'topup'::feed_type, u.nickname || ' 充值了 ' ||
       CASE WHEN t.amount < 5000 THEN '1 千至 5 千元' WHEN t.amount < 20000 THEN '5 千至 2 万元' ELSE '2 万元以上' END,
       NULL,
       CASE WHEN t.amount < 5000 THEN '1k-5k' WHEN t.amount < 20000 THEN '5k-20k' ELSE '20k+' END,
       true, t.reviewed_at
FROM topup_orders t JOIN users u ON u.id = t.buyer_id WHERE t.is_demo
UNION ALL
SELECT 'listing_created'::feed_type, u.nickname || ' 上架了 ' || m.display_name, l.model_id, NULL, true, l.created_at
FROM listings l JOIN users u ON u.id = l.seller_id JOIN models m ON m.id = l.model_id WHERE l.is_demo
UNION ALL
SELECT 'settlement_paid'::feed_type, u.nickname || ' 收到一期结算款', NULL,
       CASE WHEN s.payable < 1000 THEN '<1k' WHEN s.payable < 10000 THEN '1k-10k' ELSE '10k+' END, true, s.paid_at
FROM settlements s JOIN users u ON u.id = s.seller_id WHERE s.is_demo AND s.status = 'paid' AND s.payable > 0
UNION ALL
SELECT 'demand_posted'::feed_type, u.nickname || ' 发布了 ' || m.display_name || ' 的求购', d.model_id, NULL, true, d.created_at
FROM demand_requests d JOIN users u ON u.id = d.buyer_id JOIN models m ON m.id = d.model_id WHERE d.is_demo;

COMMIT;

-- 快速自检
SELECT 'users' t, COUNT(*) FROM users WHERE is_demo
UNION ALL SELECT 'provider_keys', COUNT(*) FROM provider_keys WHERE is_demo
UNION ALL SELECT 'listings', COUNT(*) FROM listings WHERE is_demo
UNION ALL SELECT 'usage_logs', COUNT(*) FROM usage_logs WHERE is_demo
UNION ALL SELECT 'settlements', COUNT(*) FROM settlements WHERE is_demo
UNION ALL SELECT 'activity_feed', COUNT(*) FROM activity_feed WHERE is_demo;
