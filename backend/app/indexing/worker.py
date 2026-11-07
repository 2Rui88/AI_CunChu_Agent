"""
向量化任务消费循环 —— 进程内后台任务，由 main.lifespan 启停

模型：一个进程一个循环 + indexing_concurrency 个并发槽。
Gunicorn 多 worker 部署时每进程各跑一份：领取走「条件 UPDATE 抢占」，
多消费者安全；全局消费并发 = 进程数 × indexing_concurrency。

每 30 秒顺带执行一次超时回收（消费者崩溃后的 running 行），
单进程的回收在其他进程也照常生效（幂等 UPDATE）。
"""
import asyncio
import logging
import os
import socket
import time

from app.config import settings
from app.database import Session
from app.indexing import scheduler
from app.indexing.pipeline import run_indexing_job

logger = logging.getLogger("uvicorn.error")

_RECLAIM_INTERVAL_SECONDS = 30

_worker_task: asyncio.Task | None = None
_worker_id = f"{socket.gethostname()}:{os.getpid()}"


async def _process(job) -> None:
    """执行单个任务并收敛终态（重试/失败由 scheduler.finish 决定）"""
    try:
        ok, error = await run_indexing_job(job)
    except Exception as exc:
        logger.exception("indexing job crashed: md5=%s", job.md5)
        ok, error = False, f"{type(exc).__name__}: {exc}"
    try:
        async with Session() as db:
            status = await scheduler.finish(db, job.id, ok, error)
        if status == "done":
            logger.info("indexing job done: md5=%s user=%s", job.md5, job.user)
        elif status == "pending":
            logger.warning("indexing job requeued (attempt %d): md5=%s err=%s",
                           job.attempts, job.md5, error)
        else:
            logger.error("indexing job failed permanently: md5=%s err=%s", job.md5, error)
    except Exception:
        logger.exception("indexing job finish failed: md5=%s（将依赖超时回收）", job.md5)


async def _run_loop() -> None:
    slots = asyncio.Semaphore(max(1, settings.indexing_concurrency))
    next_reclaim = 0.0
    logger.info("indexing worker started: %s concurrency=%d",
                _worker_id, settings.indexing_concurrency)
    while True:
        try:
            now = time.monotonic()
            if now >= next_reclaim:
                async with Session() as db:
                    await scheduler.reclaim_stale(db)
                next_reclaim = now + _RECLAIM_INTERVAL_SECONDS

            async with Session() as db:
                job = await scheduler.claim_next(db, _worker_id)
            if job is None:
                await asyncio.sleep(settings.indexing_poll_seconds)
                continue
            async with slots:  # 槽满时在领取后排队等待（任务已抢占，不会被别人偷走）
                await _process(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("indexing worker loop error")
            await asyncio.sleep(settings.indexing_poll_seconds)


def start_worker() -> None:
    """由应用 lifespan 调用：启动本进程的消费循环（幂等）"""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_loop())


async def stop_worker() -> None:
    """优雅停止：取消循环并等待收尾（进行中的任务由超时回收兜底）"""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _worker_task = None
