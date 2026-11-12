-- migration_eval.sql
-- RAG 检索评估：数据集（题目+金标）与评估运行（逐题结果+聚合指标）。
-- 新装库由 ORM create_all 自动建表，无需执行本脚本；存量库按 README 流程执行一次。

-- 评估数据集（题目独立成行，逐题参与评估）
CREATE TABLE IF NOT EXISTS `eval_datasets` (
    `id`             BIGINT       NOT NULL AUTO_INCREMENT,
    `dataset_id`     VARCHAR(64)  NOT NULL                COMMENT '对外标识 ds_+hex',
    `user`           VARCHAR(32)  NOT NULL                COMMENT '所属用户',
    `name`           VARCHAR(128) NOT NULL,
    `description`    VARCHAR(512)          DEFAULT '',
    `item_count`     INT          NOT NULL DEFAULT 0,
    `has_gold`       SMALLINT     NOT NULL DEFAULT 0      COMMENT '1=存在带 gold_md5s 的题目',
    `has_gold_answer` SMALLINT    NOT NULL DEFAULT 0      COMMENT '1=存在带参考答案的题目',
    `create_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_eval_dataset_id` (`dataset_id`),
    KEY `idx_eval_datasets_user` (`user`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 评估数据集题目（gold_files 文件名金标在上传时已解析为 md5）
CREATE TABLE IF NOT EXISTS `eval_dataset_items` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT,
    `dataset_id`  VARCHAR(64) NOT NULL,
    `item_index`  INT         NOT NULL                COMMENT '题号（保持评估顺序稳定）',
    `query_text`  TEXT        NOT NULL,
    `gold_md5s`   JSON                 DEFAULT NULL   COMMENT '期望命中的文件 md5 列表',
    `gold_names`  JSON                 DEFAULT NULL   COMMENT '与 gold_md5s 平行的展示名',
    `gold_answer` TEXT                 DEFAULT NULL   COMMENT '参考答案（留给答案评估阶段）',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_eval_item_dataset_index` (`dataset_id`, `item_index`),
    KEY `idx_eval_items_dataset` (`dataset_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 评估运行（逐题推进由 /eval/run/step 驱动，无后台任务）
CREATE TABLE IF NOT EXISTS `eval_runs` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT,
    `run_id`      VARCHAR(64)  NOT NULL                COMMENT '对外标识 run_+hex',
    `dataset_id`  VARCHAR(64)  NOT NULL,
    `user`        VARCHAR(32)  NOT NULL,
    `name`        VARCHAR(128)          DEFAULT '',
    `status`      VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running / completed',
    `top_k`       INT          NOT NULL DEFAULT 10,
    `config`      JSON                  DEFAULT NULL   COMMENT '启动时检索配置快照（模型/阈值/重排参数）',
    `metrics`     JSON                  DEFAULT NULL   COMMENT '完成后的聚合指标',
    `total_items` INT          NOT NULL DEFAULT 0,
    `create_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `finish_time` TIMESTAMP    NULL     DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_eval_run_id` (`run_id`),
    KEY `idx_eval_runs_user` (`user`),
    KEY `idx_eval_runs_status` (`status`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 评估逐题结果（冗余快照 query/gold，数据集删除后 run 自包含）
CREATE TABLE IF NOT EXISTS `eval_run_items` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT,
    `run_id`      VARCHAR(64) NOT NULL,
    `item_index`  INT         NOT NULL,
    `query_text`  TEXT        NOT NULL,
    `gold_md5s`   JSON                 DEFAULT NULL,
    `gold_names`  JSON                 DEFAULT NULL,
    `gold_answer` TEXT                 DEFAULT NULL,
    `retrieved`   JSON                 DEFAULT NULL   COMMENT '当次检索 Top-K 明细（两路排名+融合分数）',
    `metrics`     JSON                 DEFAULT NULL   COMMENT '单题 recall@K / f1@K / MRR',
    `error`       VARCHAR(512)          DEFAULT ''    COMMENT '非空=该题执行失败，不参与聚合',
    `create_time` TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_eval_run_item_run_index` (`run_id`, `item_index`),
    KEY `idx_eval_run_items_run` (`run_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
