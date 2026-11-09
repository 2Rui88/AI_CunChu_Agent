"""
消费循环并发模型测试 —— 领单/执行分离 + 信号量任务槽 + 租约心跳

覆盖场景：
1.  槽满时不领取（按需领取：先占槽再 claim，DB 中 running 数 ≤ 槽数）
2.  任务完成后释放槽并恢复领取
3.  进程内真并发：concurrency=2 时两个任务同时执行（派发后立即回去领下一个）
4.  领取抛异常 → 必还槽（否则槽位泄漏导致消费停摆）
5.  _run_job 异常路径必还槽（_process 围栏失效时 finally 兜底）
6.  stop_worker：在途协程已结束 → 干净返回，无残留
7.  stop_worker：在途协程卡死 → 排空超时后放弃（交给超时回收兜底）
8.  心跳按间隔周期续约；执行结束后心跳同死（不再续约）
9.  renew 返回 finished → 心跳安静退出，不取消执行
10. 偶发 DB 异常单次容忍（不触发止损）
11. 连续 3 次报到失败 → 取消执行止损且槽位仍归还
12. renew 返回 lost → 取消执行止损且槽位仍归还
13. 短任务（< 心跳间隔）一生零续约调用
"""
import asyncio
import contextlib

import pytest

from app.indexing import scheduler, worker
from app.indexing.models import IndexingJob


# ── 测试辅助 ──

class _FakeSessionCtx:
    """模拟 async with Session()：调度器已被桩掉，哑 session 仅供语法通过"""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class _FakeScheduler:
    """按脚本回放的伪调度器：script 每项为 任务对象 / None / 待抛异常；耗尽后回 None"""

    def __init__(self, script=()):
        self.script = list(script)
        self.claims = 0

    async def reclaim_stale(self, db):
        return 0

    async def claim_next(self, db, worker_id):
        self.claims += 1
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item


def _job(md5="b" * 32):
    return IndexingJob(md5=md5, user="u1", filename="f.pdf", file_type="pdf",
                       status="running", attempts=1)


def _gated_process(started: asyncio.Event, release: asyncio.Event, sink: list):
    """可阻塞的 _process 桩：进入即记录并放行 started，卡在 release 等待"""
    async def _process(job):
        sink.append(job)
        started.set()
        await release.wait()
    return _process


def _traced_process(release: asyncio.Event):
    """可追踪三态的 _process 桩：记录 started/cancelled/done（供心跳测试断言）"""
    state = {"started": False, "cancelled": False, "done": False}

    async def _process(job):
        state["started"] = True
        try:
            await release.wait()
            state["done"] = True
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
    return _process, state


def _renew_stub(script: list, calls: list):
    """按脚本回放的 renew_lease 桩：记录调用，脚本耗尽后回 renewed"""
    async def _renew(db, job_id, worker_id):
        calls.append(worker_id)
        item = script.pop(0) if script else scheduler.LEASE_RENEWED
        if isinstance(item, Exception):
            raise item
        return item
    return _renew


async def _wait_until(predicate, timeout=2.0, what="condition"):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _fast_settings(monkeypatch):
    """轮询间隔收敛到 10ms，测试不因 2s 轮询拖慢"""
    monkeypatch.setattr(worker.settings, "indexing_poll_seconds", 0.01)


@pytest.fixture(autouse=True)
async def _isolate_worker_globals():
    """模块级状态跨测试隔离；收尾掐掉遗留任务防泄漏到其他测试"""
    worker._worker_task = None
    worker._inflight.clear()
    yield
    if worker._worker_task is not None:
        worker._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker._worker_task
    for task in list(worker._inflight):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    worker._worker_task = None
    worker._inflight.clear()


def _wire(monkeypatch, sched, process=None):
    monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(worker.scheduler, "reclaim_stale", sched.reclaim_stale)
    monkeypatch.setattr(worker.scheduler, "claim_next", sched.claim_next)

    async def _no_stats():  # 领单循环首迭代即打水位日志，桩掉避免触真实 DB
        pass

    monkeypatch.setattr(worker, "_log_queue_stats", _no_stats)
    if process is not None:
        monkeypatch.setattr(worker, "_process", process)


@contextlib.asynccontextmanager
async def _loop():
    task = asyncio.create_task(worker._run_loop())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ═══════════════════════════════════════════════════════════
#  1/2. 按需领取：槽满不 claim，完成后恢复
# ═══════════════════════════════════════════════════════════

class TestSlotDiscipline:

    async def test_no_claim_while_slot_held(self, monkeypatch):
        """场景 1：唯一槽位被占 → 不再调用 claim_next"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 1)
        started, release, sink = asyncio.Event(), asyncio.Event(), []
        sched = _FakeScheduler([_job("1" * 32)])  # 领一次有任务，之后永远空
        _wire(monkeypatch, sched, _gated_process(started, release, sink))

        async with _loop():
            await _wait_until(started.is_set, what="首任务派发")
            await asyncio.sleep(0.1)  # 给循环机会违规领单（若有 bug 此处暴露）
            assert sched.claims == 1  # 槽满：按需领取，不预支任务

            release.set()  # 放行首任务 → 还槽
            await _wait_until(lambda: sched.claims >= 2, what="槽释放后恢复领取")
            assert len(sink) == 1    # 全程只执行过这一个任务

    async def test_claim_resumes_after_completion(self, monkeypatch):
        """场景 2：任务完成释放槽 → 循环继续领取下一个任务"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 1)
        started1, release1 = asyncio.Event(), asyncio.Event()
        started2, release2 = asyncio.Event(), asyncio.Event()
        sink = []
        gate1 = _gated_process(started1, release1, sink)

        async def process(job):  # 第二个任务直接完成（不卡）
            if len(sink) == 1:
                sink.append(job)
                started2.set()
                return
            await gate1(job)

        sched = _FakeScheduler([_job("1" * 32), _job("2" * 32)])
        _wire(monkeypatch, sched, process)

        async with _loop():
            await _wait_until(started1.is_set, what="首任务派发")
            assert sched.claims == 1
            release1.set()  # 首任务完成还槽
            await _wait_until(started2.is_set, what="第二任务被领取执行")
            assert sched.claims >= 2  # 第二任务完成后循环可能已做空轮询
            assert [j.md5 for j in sink] == ["1" * 32, "2" * 32]


# ═══════════════════════════════════════════════════════════
#  3. 进程内真并发
# ═══════════════════════════════════════════════════════════

class TestRealConcurrency:

    async def test_two_jobs_run_simultaneously(self, monkeypatch):
        """场景 3：concurrency=2 → 两个任务同时执行（旧串行实现此断言必挂）"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 2)
        started, release, sink = asyncio.Event(), asyncio.Event(), []
        sched = _FakeScheduler([_job("1" * 32), _job("2" * 32)])
        _wire(monkeypatch, sched, _gated_process(started, release, sink))

        async with _loop():
            await _wait_until(lambda: len(sink) == 2, what="两个任务同时进入执行")
            assert sched.claims == 2  # 首任务未完成时第二个已被领取派发
            release.set()


# ═══════════════════════════════════════════════════════════
#  4/5. 异常路径必还槽
# ═══════════════════════════════════════════════════════════

class TestSlotReleaseOnError:

    async def test_claim_failure_releases_slot(self, monkeypatch):
        """场景 4：claim_next 抛异常 → 槽已还，循环存活并继续领取"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 1)
        started, release, sink = asyncio.Event(), asyncio.Event(), []
        sched = _FakeScheduler([ConnectionError("db down"), _job("3" * 32)])
        _wire(monkeypatch, sched, _gated_process(started, release, sink))

        async with _loop():
            # 若领取失败后槽未还，第二次 acquire 将永久挂起 → 此处超时暴露
            await _wait_until(started.is_set, what="异常后恢复领取并执行任务")
            assert sched.claims == 2
            release.set()

    async def test_run_job_releases_slot_on_error(self, monkeypatch):
        """场景 5：_process 异常逃逸围栏 → _run_job 吞掉异常返回（派发任务不得
        逃逸异常），finally 仍还槽"""
        async def boom(job):
            raise RuntimeError("fence broken")

        monkeypatch.setattr(worker, "_process", boom)
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        slots = asyncio.Semaphore(1)
        await slots.acquire()  # 占满唯一槽位

        await asyncio.wait_for(worker._run_job(_job(), slots), timeout=2)  # 不抛出

        assert not slots.locked()  # 槽已归还
        await asyncio.wait_for(slots.acquire(), timeout=0.1)  # 可立即再次获取
        slots.release()


# ═══════════════════════════════════════════════════════════
#  6/7. stop_worker 优雅停机
# ═══════════════════════════════════════════════════════════

class TestStopWorker:

    async def test_clean_stop_after_drain(self, monkeypatch):
        """场景 6：在途协程结束 → stop_worker 干净返回，无残留"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 2)
        started, release, sink = asyncio.Event(), asyncio.Event(), []
        sched = _FakeScheduler([_job("4" * 32)])
        _wire(monkeypatch, sched, _gated_process(started, release, sink))

        worker.start_worker()
        await _wait_until(started.is_set, what="任务派发")
        release.set()
        await asyncio.sleep(0.05)  # 让完成回调把 _inflight 清干净

        await asyncio.wait_for(worker.stop_worker(), timeout=3)
        assert worker._worker_task is None
        assert not worker._inflight

    async def test_stop_gives_up_on_stuck_job(self, monkeypatch):
        """场景 7：在途协程卡死 → 排空超时后放弃，不停等（交给超时回收兜底）"""
        monkeypatch.setattr(worker.settings, "indexing_concurrency", 1)
        monkeypatch.setattr(worker, "_DRAIN_TIMEOUT_SECONDS", 0.05)
        started, release, sink = asyncio.Event(), asyncio.Event(), []
        sched = _FakeScheduler([_job("5" * 32)])
        _wire(monkeypatch, sched, _gated_process(started, release, sink))

        worker.start_worker()
        await _wait_until(started.is_set, what="任务派发")
        stuck = next(iter(worker._inflight))  # 停机前留住句柄，便于测试收尾

        await asyncio.wait_for(worker.stop_worker(), timeout=2)  # 不受 5s 默认值拖累
        assert worker._worker_task is None
        assert not worker._inflight
        assert not stuck.done()  # 卡死协程不被强杀，留给超时回收

        release.set()  # 测试收尾：放行并回收协程
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stuck, timeout=1)


# ═══════════════════════════════════════════════════════════
#  8-13. 任务租约心跳（执行/心跳双协程护航）
# ═══════════════════════════════════════════════════════════

class TestLeaseHeartbeat:

    async def test_heartbeat_renews_periodically_then_dies_with_job(
            self, monkeypatch):
        """场景 8：执行期间按间隔续约；执行结束后心跳同死，不再续约"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.02)
        release = asyncio.Event()
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease", _renew_stub([], calls))

        slots = asyncio.Semaphore(1)
        runner = asyncio.create_task(worker._run_job(_job(), slots))
        await _wait_until(lambda: len(calls) >= 2, what="周期续约至少两次")
        assert not state["cancelled"]

        release.set()  # 任务正常完成 → 心跳应在 finally 被回收
        await asyncio.wait_for(runner, timeout=2)
        snapshot = len(calls)
        await asyncio.sleep(0.08)  # 留出 4 个心跳周期，确认不再续约
        assert len(calls) == snapshot
        assert state["done"] and not slots.locked()

    async def test_finished_state_is_quiet(self, monkeypatch):
        """场景 9：renew 返回 finished（自己刚收尾）→ 心跳安静退出，不取消执行"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.02)
        release = asyncio.Event()
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease",
                            _renew_stub([scheduler.LEASE_FINISHED], calls))

        slots = asyncio.Semaphore(1)
        runner = asyncio.create_task(worker._run_job(_job(), slots))
        await _wait_until(lambda: len(calls) == 1, what="首次心跳返回 finished")
        await asyncio.sleep(0.08)  # 若误判取消，state["cancelled"] 会在此内置位
        assert not state["cancelled"] and not state["done"]

        release.set()  # 仍应正常跑完
        await asyncio.wait_for(runner, timeout=2)
        assert state["done"] and not slots.locked()

    async def test_single_db_glitch_tolerated(self, monkeypatch):
        """场景 10：偶发 DB 异常 1 次不触发止损，续约恢复后任务照常完成"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.02)
        release = asyncio.Event()
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease",
                            _renew_stub([ConnectionError("db jitter")], calls))

        slots = asyncio.Semaphore(1)
        runner = asyncio.create_task(worker._run_job(_job(), slots))
        await _wait_until(lambda: len(calls) >= 3, what="抖动后恢复续约")
        assert not state["cancelled"]

        release.set()
        await asyncio.wait_for(runner, timeout=2)
        assert state["done"] and not slots.locked()

    async def test_three_consecutive_misses_cancel_and_release(self, monkeypatch):
        """场景 11：连续 3 次报到失败 → 按丢租约取消执行止损，槽位仍归还"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.02)
        release = asyncio.Event()  # 永不置位：模拟长任务在途
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease",
                            _renew_stub([ConnectionError("db down")] * 3, calls))

        slots = asyncio.Semaphore(1)
        runner = asyncio.create_task(worker._run_job(_job(), slots))
        await asyncio.wait_for(runner, timeout=2)  # 止损后 _run_job 自行返回
        assert len(calls) == worker._LEASE_MISS_TOLERANCE
        assert state["cancelled"] and not state["done"]
        assert not slots.locked()  # 丢租约取消路径同样必还槽

    async def test_lost_state_cancels_and_releases(self, monkeypatch):
        """场景 12：renew 返回 lost（行被回收/接管）→ 取消执行止损，槽位仍归还"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.02)
        release = asyncio.Event()
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease",
                            _renew_stub([scheduler.LEASE_LOST], calls))

        slots = asyncio.Semaphore(1)
        runner = asyncio.create_task(worker._run_job(_job(), slots))
        await asyncio.wait_for(runner, timeout=2)
        assert len(calls) == 1
        assert state["cancelled"] and not state["done"]
        assert not slots.locked()

    async def test_short_job_never_renews(self, monkeypatch):
        """场景 13：先睡后跳——心跳间隔内完成的短任务一生零续约调用"""
        monkeypatch.setattr(worker.settings, "indexing_lease_renew_seconds", 0.05)
        release = asyncio.Event()
        release.set()  # 任务立即完成
        process, state = _traced_process(release)
        calls: list = []
        monkeypatch.setattr(worker, "Session", lambda: _FakeSessionCtx())
        monkeypatch.setattr(worker, "_process", process)
        monkeypatch.setattr(worker.scheduler, "renew_lease", _renew_stub([], calls))

        slots = asyncio.Semaphore(1)
        await asyncio.wait_for(worker._run_job(_job(), slots), timeout=2)
        await asyncio.sleep(0.2)  # 留出 4 个心跳周期，确认没有事后续约
        assert calls == []
        assert state["done"] and not slots.locked()
