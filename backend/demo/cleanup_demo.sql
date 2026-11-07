-- =====================================================================
-- 一键清除所有演示数据（is_demo = true）
-- 顺序按外键依赖从子表到父表；跑完后 SELECT 应全部为 0
-- =====================================================================
BEGIN;

DELETE FROM activity_feed       WHERE is_demo;
DELETE FROM demand_requests     WHERE is_demo;
DELETE FROM delivery_orders     WHERE is_demo;
DELETE FROM disputes            WHERE is_demo;
DELETE FROM credit_events       WHERE is_demo;
DELETE FROM deductions          WHERE is_demo;
DELETE FROM payout_requests     WHERE is_demo;
DELETE FROM settlements         WHERE is_demo;
DELETE FROM usage_hourly        WHERE is_demo;
DELETE FROM usage_logs          WHERE is_demo;
DELETE FROM wallet_transactions WHERE is_demo;
DELETE FROM topup_orders        WHERE is_demo;
DELETE FROM api_tokens          WHERE is_demo;
DELETE FROM key_probes          WHERE is_demo;
DELETE FROM listings            WHERE is_demo;
DELETE FROM provider_keys       WHERE is_demo;
DELETE FROM hardware_listings   WHERE is_demo;
DELETE FROM pricing_rules       WHERE is_demo;
DELETE FROM wallets             WHERE user_id IN (SELECT id FROM users WHERE is_demo);
DELETE FROM seller_profiles     WHERE is_demo;
DELETE FROM users               WHERE is_demo;
DELETE FROM models              WHERE is_demo;      -- 如需保留模型目录，注释掉这两行
DELETE FROM providers           WHERE is_demo;

COMMIT;

SELECT 'users' t, COUNT(*) FROM users WHERE is_demo
UNION ALL SELECT 'usage_logs', COUNT(*) FROM usage_logs WHERE is_demo
UNION ALL SELECT 'activity_feed', COUNT(*) FROM activity_feed WHERE is_demo;
