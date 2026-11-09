"""Gunicorn 生产配置 —— FastAPI + UvicornWorker"""

import multiprocessing
import os

# 绑定地址
bind = "0.0.0.0:8000"

# Worker 数量（默认 CPU 核心数 × 2 + 1；按角色覆盖：HTTP 池 3 / 任务池 5）
workers = int(os.getenv("GUNICORN_WORKERS", str(min(multiprocessing.cpu_count() * 2 + 1, 8))))

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

# 定期回收 Worker（防止内存泄漏累积）；任务池置 GUNICORN_MAX_REQUESTS=0
# 永不回收——回收会掐断在途任务，只能靠超时回收重跑兜底，白白浪费执行
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "10000"))
max_requests_jitter = 1000
