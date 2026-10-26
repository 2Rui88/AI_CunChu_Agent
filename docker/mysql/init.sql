-- AI-YunCunChu 初始化建表脚本
-- 与 backend/app/models.py 中的 ORM 定义保持一致（create_all 亦为幂等兜底）
-- 由 docker-entrypoint-initdb.d 在 MySQL 数据卷首次初始化时自动执行

CREATE TABLE IF NOT EXISTS user_info (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_name VARCHAR(32) NOT NULL UNIQUE,
    nick_name VARCHAR(32) NOT NULL UNIQUE,
    password VARCHAR(128) NOT NULL,              -- bcrypt 哈希 60 字符；旧 MD5 为 32 字符
    salt VARCHAR(32) NOT NULL,                   -- 仅旧算法使用，bcrypt 自带盐
    algo VARCHAR(16) NOT NULL DEFAULT 'md5',     -- 密码算法: md5(旧)/bcrypt
    phone VARCHAR(16) DEFAULT '',
    email VARCHAR(64) DEFAULT '',
    api_key VARCHAR(256) DEFAULT '',
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS file_info (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    md5 VARCHAR(256) NOT NULL UNIQUE,
    file_id VARCHAR(256) NOT NULL,
    url VARCHAR(512) NOT NULL,
    size BIGINT DEFAULT 0,
    type VARCHAR(32) DEFAULT '',
    count INT DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_file_list (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user VARCHAR(32) NOT NULL,
    md5 VARCHAR(256) NOT NULL,
    file_name VARCHAR(128),
    shared_status INT DEFAULT 0,
    pv INT DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_ufl_user (user),
    KEY idx_ufl_user_md5 (user, md5(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS share_file_list (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user VARCHAR(32) NOT NULL,
    md5 VARCHAR(256) NOT NULL,
    file_name VARCHAR(128),
    pv INT DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_share_md5_filename (md5, file_name),
    KEY idx_sfl_user (user),
    KEY idx_sfl_md5 (md5(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_file_count (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user VARCHAR(128) NOT NULL UNIQUE,
    count INT DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS file_ai_desc (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    md5 VARCHAR(256) NOT NULL,
    chunk_index INT DEFAULT 0 COMMENT '切片序号，0 表示未分块',
    description TEXT NOT NULL,
    embedding MEDIUMBLOB,
    faiss_id INT DEFAULT -1,
    context_label VARCHAR(256) DEFAULT '' COMMENT '切片上下文标签',
    model VARCHAR(64) DEFAULT '',
    status SMALLINT DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_md5_chunk (md5(191), chunk_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_file_ai_desc (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user VARCHAR(32) NOT NULL,
    md5 VARCHAR(256) NOT NULL,
    chunk_index INT DEFAULT 0 COMMENT '切片序号，0 表示未分块',
    cache_id BIGINT,
    description TEXT NOT NULL,
    embedding MEDIUMBLOB,
    faiss_id INT DEFAULT -1,
    context_label VARCHAR(256) DEFAULT '' COMMENT '切片上下文标签',
    model VARCHAR(64) DEFAULT '',
    status SMALLINT DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_user_md5_chunk (user, md5(191), chunk_index),
    KEY idxufad_user (user)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
