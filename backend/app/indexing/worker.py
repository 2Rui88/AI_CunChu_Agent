"""
向量化任务消费循环 —— 进程内后台任务，由 main.lifespan 启停（角色开关控制）

模型：一个进程一个领单循环 + indexing_concurrency 个任务槽。
领单与执行分离：先占空槽再领任务（DB 中 running 数 ≤ 槽数，进程崩溃
损失面最小），领到后派发独立协程执行、立即回去领下一个——进程内真并发。
领取走「条件 UPDATE 抢占」，多消费者安全（HTTP 池与任务池混跑亦然）；
全局消费并发 = 消费进程数 × indexing_concurrency。

每个任务由「执行 + 心跳」双协程护航：心跳周期续租 started_at，防长任务
（合法运行超过超时阈值）被超时回收误判死亡而重投双跑；丢租约即取消执行
止损（不再白白消耗 API）。心跳只证明进程活着，不感知任务进度。

每 30 秒顺带执行一次超时回收（消费者崩溃后的 running 行），每 300 秒
输出一行队列水位日志；单进程的回收在其他进程也照常生效（幂等 UPDATE）。
"""
import asyncio
import contextlib
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
_QUEUE_LOG_INTERVAL_SECONDS = 300  # 队列水位日志节奏（灰度期直接看 docker logs）
_DRAIN_TIMEOUT_SECONDS = 5  # 停机等待在途协程的上限，等不完交给超时回收兜底
# 心跳连续报到失败上限：偶发 DB 抖动容忍（不误杀），长时间失联止损（不白烧钱）
_LEASE_MISS_TOLERANCE = 3

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
            # worker 参数取任务行自带的 job.worker（claim_next 抢占时已写好）；
            # 丢租约时 finish 返回 None（行已被回收/接管，本进程不写终态）
            status = await scheduler.finish(db, job.id, ok, job.worker, error)
        if status == "done":
            logger.info("indexing job done: md5=%s user=%s", job.md5, job.user)
        elif status == "pending":
            logger.warning("indexing job requeued (attempt %d): md5=%s err=%s",
                           job.attempts, job.md5, error)
        elif status is None:
            logger.warning("indexing job lease lost before finish: md5=%s（结果作废，"
                           "由新持有者收敛）", job.md5)
        else:
            logger.error("indexing job failed permanently: md5=%s err=%s", job.md5, error)
    except Exception:
        logger.exception("indexing job finish failed: md5=%s（将依赖超时回收）", job.md5)


async def _log_queue_stats() -> None:
    """输出一行队列水位（一条聚合 SQL 的代价；堵"容器 up 但没人消费"盲区）"""
    try:
        async with Session() as db:
            q = await scheduler.queue_stats(db)
        logger.info("indexing queue: pending=%d running=%d failed=%d oldest_pending=%ds",
                    q["pending"], q["running"], q["failed"], q["oldest_pending_seconds"])
    except Exception as exc:
        logger.warning("indexing queue stats failed: %s", exc)


async def _run_loop() -> None:
    slots = asyncio.Semaphore(max(1, settings.indexing_concurrency))
    next_reclaim = 0.0
    next_queue_log = 0.0
    logger.info("indexing worker started: %s concurrency=%d",
                _worker_id, settings.indexing_concurrency)
    while True:
        try:
            now = time.monotonic()
            if now >= next_reclaim:
                async with Session() as db:
                    await scheduler.reclaim_stale(db)
                next_reclaim = now + _RECLAIM_INTERVAL_SECONDS
            if now >= next_queue_log:
                await _log_queue_stats()
                next_queue_log = now + _QUEUE_LOG_INTERVAL_SECONDS

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


async def _heartbeat(job, lease_lost: asyncio.Event) -> None:
    """
    任务租约心跳：执行期间周期续租 started_at，与执行协程同生共死
    （由 _run_job 的 finally 确定回收，不进 _inflight——那是执行协程的
    防 GC 集合）。

    先睡后跳：60s 间隔内完成的短任务一生零心跳（claim 抢占写入的
    started_at 本身就是第 0 次报到）。数字账：心跳 60s / 超时 900s =
    一个租约期 15 次续约机会；死亡检测延迟 = 最后心跳 + 900s（与无
    心跳时一致，零退化）；满载 20 并发额外负载 ≈ 0.33 条 UPDATE/s。

    三种退出：
    - renewed：续约成功，连续失败清零，继续
    - finished：行已收敛终态（自己的执行协程刚收尾）——安静退出，不打扰
    - lost / 连续 _LEASE_MISS_TOLERANCE 次报到失败：置 lease_lost 交由
      _run_job 取消执行止损
    """
    misses = 0
    while True:
        await asyncio.sleep(settings.indexing_lease_renew_seconds)
        try:
            async with Session() as db:
                state = await scheduler.renew_lease(db, job.id, job.worker)
        except Exception:
            misses += 1
            if misses >= _LEASE_MISS_TOLERANCE:
                logger.warning("indexing lease renew failed %d times: md5=%s"
                               "（按丢租约止损）", misses, job.md5)
                lease_lost.set()
                return
            continue  # 偶发 DB 抖动容忍，不误杀
        if state == scheduler.LEASE_RENEWED:
            misses = 0
            continue
        if state == scheduler.LEASE_FINISHED:
            return  # 自己刚收尾，安静退出
        logger.warning("indexing lease lost: md5=%s worker=%s（行已被回收/接管）",
                       job.md5, job.worker)
        lease_lost.set()
        return


async def _run_job(job, slots: asyncio.Semaphore) -> None:
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(job, lease_lost))
    try:
        execute = asyncio.create_task(_process(job))
        watcher = asyncio.create_task(lease_lost.wait())
        try:
            await asyncio.wait({execute, watcher},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        if lease_lost.is_set() and not execute.done():
            execute.cancel()  # 止损：行已被回收重投，继续跑只会重复消耗 API
            logger.warning("indexing job cancelled on lease loss (已取消止损): "
                           "md5=%s worker=%s", job.md5, job.worker)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await execute  # 回收执行协程（CancelledError 不被 _process 围栏吞掉）
    finally:
        heartbeat.cancel()  # 心跳与执行同生共死
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
        slots.release()  # 无论成败/丢租约必还槽，槽位永不泄漏


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
