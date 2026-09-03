-- 给 tech_oncall 排查机器人建一个只读账号（Postgres）。
--
-- 目标库确认信息（来自 ai4all_bridge 生产 .env 里的 DATABASE_URL，engine=postgres 已确认）：
--   host=127.0.0.1  port=55432  dbname=ai4all_dev
-- 这就是 ai4all-bridge-backend.service 实际连接的库——注意库名叫 ai4all_dev，
-- 跟 compose.dev.yml 起的本地开发库是同一个实例，这台机器上目前没有另一个独立的
-- "生产库"。如果这不符合预期（比如你以为应该有个独立的生产 Postgres），执行前先确认一下。
--
-- 用法：
--   1. 把下面 CREATE ROLE 那行的密码占位符换成你自己生成的强密码
--   2. 用现有的管理员/app 账号执行：
--        psql "postgresql://ai4all:<管理员密码>@127.0.0.1:55432/ai4all_dev" -f deploy/create_readonly_db_role.sql
--   3. 把新账号的 DSN 写进 /opt/workspace/tech_oncall/.env，例如：
--        DB_DSN_AI4ALL_BRIDGE=postgresql://tech_oncall_ro:<你设的密码>@127.0.0.1:55432/ai4all_dev
--      告诉我这个变量名，我把 config/targets.yaml 的 databases 条目补上就行
--
-- 安全设计：
--   - default_transaction_read_only=on 是 Postgres 引擎级别强制的只读——哪怕 tech_oncall
--     代码有 bug 漏了 SELECT-only 校验，这个账号本身在数据库层面也写不进去，这是真正的硬边界
--     （src/tools/db_tools.py 里的 SELECT-only 正则校验 + BEGIN READ ONLY 只是代码层的第二道防线）
--   - CONNECTION LIMIT 限制并发连接数，避免排查 Agent 占满连接池（app 自己 DB_POOL_MAX_SIZE=8）

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tech_oncall_ro') THEN
        CREATE ROLE tech_oncall_ro WITH LOGIN PASSWORD '<REPLACE_WITH_STRONG_PASSWORD>';
    END IF;
END
$$;

ALTER ROLE tech_oncall_ro SET default_transaction_read_only = on;
ALTER ROLE tech_oncall_ro CONNECTION LIMIT 3;

GRANT CONNECT ON DATABASE ai4all_dev TO tech_oncall_ro;
GRANT USAGE ON SCHEMA public TO tech_oncall_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO tech_oncall_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO tech_oncall_ro;
