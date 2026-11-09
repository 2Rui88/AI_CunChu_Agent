"""
向量化任务消费循环 —— 进程内后台任务，由 main.lifespan 启停（角色开关控制）

模型：一个进程一个领单循环 + indexing_concurrency 个任务槽。
领单与执行分离：先占空槽再领任务（DB 中 running 数 ≤ 槽数，进程崩溃
损失面最小），领到后派发独立协程执行、立即回去领下一个——进程内真并发。
领取走「条件 UPDATE 抢占」，多消费者安全（HTTP 池与任务池混跑亦然）；
全局消费并发 = 消费进程数 × indexing_concurrency。

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
_DRAIN_TIMEOUT_SECONDS = 5  # 停机等待在途协程的上限，等不完交给超时回收兜底

_worker_task: asyncio.Task | None = None
# 在途执行协程：持有引用防 GC 中途回收；stop_worker 据此优雅排空
_inflight: set[asyncio.Task] = set()

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

            await slots.acquire()  # 先占空槽再领任务：槽满挂起，不预支任务
            try:
                async with Session() as db:
                    job = await scheduler.claim_next(db, _worker_id)
            except Exception:
                slots.release()  # 领取失败必还槽，否则槽位泄漏导致消费停摆
                raise
            if job is None:
                slots.release()  # 队列空：还槽歇一轮
                await asyncio.sleep(settings.indexing_poll_seconds)
                continue

            task = asyncio.create_task(_run_job(job, slots))
            _inflight.add(task)  # 集合持有防 GC；完毕自动移除
            task.add_done_callback(_inflight.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("indexing worker loop error")
            await asyncio.sleep(settings.indexing_poll_seconds)


async def _run_job(job, slots: asyncio.Semaphore) -> None:
    try:
        await _process(job)  # _process 已有异常围栏，正常不会抛出
    finally:
        slots.release()  # 无论成败必还槽，槽位永不泄漏


def start_worker() -> None:
    """由应用 lifespan 调用：启动本进程的消费循环（幂等）"""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_loop())


async def stop_worker() -> None:
    """优雅停止：取消领单循环，等待在途执行协程最多 5 秒
    （等不完的任务不取消，交给超时回收重投兜底）"""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _worker_task = None
    if _inflight:
        _, pending = await asyncio.wait(_inflight, timeout=_DRAIN_TIMEOUT_SECONDS)
        if pending:
            logger.warning("indexing worker stop: %d job(s) still running"
                           "（交给超时回收兜底）", len(pending))
        _inflight.clear()
