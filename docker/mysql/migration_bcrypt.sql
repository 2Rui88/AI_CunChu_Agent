-- 迁移：密码哈希升级 bcrypt（存量部署执行一次；新数据卷由 init.sql 直接建新结构）
-- 说明：
--   1. password 列扩到 VARCHAR(128) 以容纳 bcrypt 的 60 字符哈希
--   2. 新增 algo 列标记算法，存量用户默认 'md5'
--   3. 存量用户无需改密码：登录验证通过后由后端透明重哈希为 bcrypt 并改标 'bcrypt'
-- 执行方式（容器内）：
--   docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < migration_bcrypt.sql

ALTER TABLE user_info
    MODIFY COLUMN password VARCHAR(128) NOT NULL,
    ADD COLUMN algo VARCHAR(16) NOT NULL DEFAULT 'md5' COMMENT '密码算法: md5(旧)/bcrypt' AFTER salt;
