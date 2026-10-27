-- 迁移：删除 user_info.api_key 遗留列
-- 密钥改为部署环境变量统一配置（DASHSCOPE_API_KEY），任何形式不再落库。
-- 执行方式（容器内）：
--   docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < migration_drop_apikey.sql

ALTER TABLE user_info DROP COLUMN api_key;
