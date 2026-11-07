-- migration_indexing_jobs.sql
-- 向量化任务异步化：上传只投递任务，重活由后台消费者执行。
-- 新装库由 ORM create_all 自动建表，无需执行本脚本；存量库按 README 流程执行一次。

-- 任务表（表即队列：pending 行 FIFO，条件 UPDATE 原子领取）
CREATE TABLE IF NOT EXISTS `indexing_jobs` (
    `id`           BIGINT       NOT NULL AUTO_INCREMENT,
    `md5`          VARCHAR(256) NOT NULL                COMMENT '文件内容 MD5，任务幂等键',
    `user`         VARCHAR(32)  NOT NULL                COMMENT '触发者（公平调度 + 展示归属）',
    `filename`     VARCHAR(128)          DEFAULT '',
    `file_type`    VARCHAR(32)           DEFAULT '',
    `force`        SMALLINT              DEFAULT 0      COMMENT '1=手动重建：清旧切片全量重做',
    `status`       VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending / running / done / failed',
    `attempts`     INT          NOT NULL DEFAULT 0      COMMENT '已执行次数（含首次）',
    `error`        VARCHAR(512)          DEFAULT ''     COMMENT '最后一次失败的错误摘要',
    `worker`       VARCHAR(64)           DEFAULT ''     COMMENT '当前/最后领取者标识',
    `started_at`   TIMESTAMP    NULL     DEFAULT NULL   COMMENT '最近一次领取时间（超时回收依据）',
    `finished_at`  TIMESTAMP    NULL     DEFAULT NULL   COMMENT '终态时间',
    `create_time`  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_indexing_job_md5` (`md5`),
    KEY `idx_indexing_jobs_user` (`user`),
    KEY `idx_indexing_jobs_status` (`status`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- AI 用量记账（审计/配额基础；只记事实，不做事前限制）
CREATE TABLE IF NOT EXISTS `ai_usage_log` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT,
    `user`        VARCHAR(32) NOT NULL                COMMENT '触发用户',
    `kind`        VARCHAR(16) NOT NULL                COMMENT 'chat / embedding / vl / rerank',
    `model`       VARCHAR(64)          DEFAULT ''     COMMENT '模型名快照',
    `tokens`      INT                  DEFAULT 0      COMMENT 'token 数（API 未返回时为 0，只记次数）',
    `calls`       INT                  DEFAULT 1      COMMENT '聚合计数（embedding 逐块合并为一次）',
    `create_time` TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_ai_usage_user_time` (`user`, `create_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
