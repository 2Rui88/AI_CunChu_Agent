"""Gunicorn 生产配置 —— FastAPI + UvicornWorker"""

import multiprocessing

# 绑定地址
bind = "0.0.0.0:8000"

# Worker 数量（CPU 核心数 × 2 + 1）
workers = min(multiprocessing.cpu_count() * 2 + 1, 8)

# 异步 Worker 类（支持 FastAPI 异步路由）
worker_class = "uvicorn.workers.UvicornWorker"

# 超时（AI 接口需较长时间）
timeout = 120
graceful_timeout = 30
keepalive = 5

# 日志输出到 stdout（Docker 收集）
accesslog = "-"
errorlog = "-"
loglevel = "info"

# 预加载应用（fork 前加载，减少内存）
preload_app = True

# 定期回收 Worker（防止内存泄漏累积）
max_requests = 10000
max_requests_jitter = 1000
