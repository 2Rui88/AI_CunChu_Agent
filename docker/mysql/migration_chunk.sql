-- 分块架构数据库迁移
-- 为 file_ai_desc 和 user_file_ai_desc 添加 chunk_index 和 context_label 字段
-- 并修改唯一约束为 (md5, chunk_index) / (user, md5, chunk_index)

ALTER TABLE file_ai_desc
    ADD COLUMN IF NOT EXISTS chunk_index INT DEFAULT 0 COMMENT '切片序号',
    ADD COLUMN IF NOT EXISTS context_label VARCHAR(256) DEFAULT '' COMMENT '上下文标签',
    DROP INDEX IF EXISTS uq_md5,
    ADD UNIQUE INDEX IF NOT EXISTS uq_md5_chunk (md5(191), chunk_index);

ALTER TABLE user_file_ai_desc
    ADD COLUMN IF NOT EXISTS chunk_index INT DEFAULT 0 COMMENT '切片序号',
    ADD COLUMN IF NOT EXISTS context_label VARCHAR(256) DEFAULT '' COMMENT '上下文标签',
    DROP INDEX IF EXISTS uq_user_md5,
    ADD UNIQUE INDEX IF NOT EXISTS uq_user_md5_chunk (user, md5(191), chunk_index);
