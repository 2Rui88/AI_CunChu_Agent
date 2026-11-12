-- migration_dept.sql
-- 部门权限隔离：departments 表、用户表加部门/角色列、文件与共享表加部门维度、
-- 共享唯一索引从全局唯一改为部门内唯一（同一文件可在不同部门各自分享）。
-- 新装库由 init.sql + ORM create_all 自动具备，无需执行本脚本；存量库按 README 流程执行一次。
-- 存量数据全部回填"默认部门(1)"且角色为 member，迁移后系统行为与迁移前完全一致。

-- 管道执行时 mysql 客户端可能以 latin1 解释本文件（含中文 INSERT），显式声明连接字符集
SET NAMES utf8mb4;

-- 部门表
CREATE TABLE IF NOT EXISTS `departments` (
    `id`          INT         NOT NULL AUTO_INCREMENT,
    `name`        VARCHAR(64) NOT NULL,
    `create_time` TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_dept_name` (`name`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

INSERT INTO `departments` (`id`, `name`) VALUES (1, '默认部门')
    ON DUPLICATE KEY UPDATE `name` = VALUES(`name`);

-- 用户：部门归属 + 角色（member 普通成员 / supervisor 主管，本期用 SQL 维护）
ALTER TABLE `user_info`
    ADD COLUMN `department_id` INT         NOT NULL DEFAULT 1 COMMENT '所属部门',
    ADD COLUMN `role`          VARCHAR(16) NOT NULL DEFAULT 'member' COMMENT 'member 普通成员 / supervisor 主管',
    ADD KEY `idx_user_dept` (`department_id`);

-- 文件：部门随上传者（冗余存储，部门文件列表不必联 user_info）
ALTER TABLE `user_file_list`
    ADD COLUMN `department_id` INT NOT NULL DEFAULT 1 COMMENT '文件所属部门（=上传者部门）',
    ADD KEY `idx_ufl_dept` (`department_id`);

-- 共享：语义从全站收窄为部门内共享
ALTER TABLE `share_file_list`
    ADD COLUMN `department_id` INT NOT NULL DEFAULT 1 COMMENT '共享所在部门',
    ADD KEY `idx_sfl_dept` (`department_id`);

-- 唯一索引：(md5, file_name) 全局唯一 → (department_id, md5, file_name) 部门内唯一
ALTER TABLE `share_file_list`
    DROP INDEX `uq_share_md5_filename`,
    ADD UNIQUE KEY `uq_share_dept_md5_filename` (`department_id`, `md5`(191), `file_name`);
