"""
向量化任务队列与限流层测试 —— 投递幂等 / 公平领取抢占 / 租约续约 / 终态收敛 / 统计 / 退避重试

覆盖场景：
1.  enqueue：非 force 且已有用户索引 → 短路 done，不建任务
2.  enqueue：无既有任务 → 新建 pending
3.  enqueue：同 md5 在途（pending/running）→ 复用，不重复建行
4.  enqueue：failed 行再次投递 → 重置 pending 且 attempts 归零
5.  claim_next：抢占 UPDATE 影响 0 行（被并发消费者抢走）→ 换下一候选
6.  claim_next：候选全部被抢 → 返回 None
7.  finish：成功（持有者匹配）→ done
8.  finish：失败且未超重试上限 → 回 pending（自动重试）
9.  finish：失败且达到重试上限 → failed 终态
10. finish：丢租约（行被回收/接管，UPDATE 0 行）→ 返回 None 不写终态
11. renew_lease：本进程持有 running 行 → renewed（条件含 worker 与 status，显式提交）
12. renew_lease：行已 done/failed → finished（安静退出信号）
13. renew_lease：行已回 pending / 被他人接管 → lost（止损信号）
14. call_with_backoff：前两次限流第三次成功 → 重试成功
15. call_with_backoff：重试耗尽 → 抛最终错误
16. call_with_backoff：非可重试异常 → 立即抛出不重试
17. Gate：Redis 异常 → 闸门降级直通（不阻塞业务）
"""
import asyncio
import types

import pytest

from app import ratelimit
from app.indexing import scheduler
from app.indexing.models import IndexingJob


# ── 测试辅助：伪 DB session（按 SQL 文本路由预设结果） ──

class _FakeResult:
    def __init__(self, scalar=None, scalars_all=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount
        self._scalars_all = scalars_all or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._scalars_all


class _FakeDB:
    """按调用顺序消费预设结果的伪 session；记录 SQL 与参数供断言"""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []          # (sql_text, params)
        self.added = []
        self.committed = 0

    async def execute(self, sql, params=None):
        sql_text = str(sql)
        self.calls.append((sql_text, params))
        # 显式按语义路由（同形状 SQL 可能来自不同分支）
        if self.results:
            item = self.results.pop(0)
            if callable(item):
                return item(sql_text, params)
            return item
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def _job(md5="a" * 32, user="u1", status="pending", attempts=0, **kw):
    return IndexingJob(md5=md5, user=user, filename="f.pdf", file_type="pdf",
                       status=status, attempts=attempts, **kw)


def _select_result(value):
    return _FakeResult(scalar=value)


def _update_result(rowcount):
    r = _FakeResult(rowcount=rowcount)
    # finish 后回读状态的 select
    r.scalar = lambda: None
    return r


# ═══════════════════════════════════════════════════════════
#  1-4. enqueue 幂等分支
# ═══════════════════════════════════════════════════════════

class TestEnqueue:

    async def test_shortcut_when_user_has_index(self):
        """场景 1：非 force 且用户已有完成索引 → 短路 done，不建任务"""
        db = _FakeDB([_select_result(123)])  # UserFileAiDesc.id 命中
        outcome, job = await scheduler.enqueue(
            db, md5="m1", user="u1", filename="f.pdf", file_type="pdf", force=False)
        assert outcome == scheduler.ALREADY_DONE
        assert db.added == []

    async def test_create_new_job(self):
        """场景 2：无用户索引、无既有任务 → 新建 pending"""
        db = _FakeDB([
            _select_result(None),   # UserFileAiDesc 未命中
            _select_result(None),   # IndexingJob 不存在
        ])
        outcome, job = await scheduler.enqueue(
            db, md5="m1", user="u1", filename="f.pdf", file_type="PDF", force=False)
        assert outcome == scheduler.QUEUED
        assert len(db.added) == 1
        assert db.added[0].status == "pending"
        assert db.added[0].file_type == "pdf"  # 归一化为小写

    async def test_reuse_inflight_job(self):
        """场景 3：同 md5 在途 → 复用现状，不覆盖触发者"""
        existing = _job(status="running", user="u2", attempts=1)
        db = _FakeDB([
            _select_result(existing),  # force=True：跳过用户索引短路，直接查任务行
        ])
        outcome, job = await scheduler.enqueue(
            db, md5="a" * 32, user="u1", filename="x.pdf", file_type="pdf", force=True)
        assert outcome == scheduler.QUEUED
        assert job["status"] == "running"
        assert existing.user == "u2"  # 在途任务的触发者不被覆盖

    async def test_reset_failed_job(self):
        """场景 4：failed 行重投 → pending 且 attempts 归零、归属更新"""
        existing = _job(status="failed", user="u2", attempts=3, error="boom")
        db = _FakeDB([
            _select_result(None),
            _select_result(existing),
        ])
        outcome, job = await scheduler.enqueue(
            db, md5="a" * 32, user="u1", filename="x.pdf", file_type="pdf", force=False)
        assert outcome == scheduler.REQUEUED
        assert existing.status == "pending"
        assert existing.attempts == 0
        assert existing.user == "u1"
        assert existing.error == ""


# ═══════════════════════════════════════════════════════════
#  5/6. claim_next 抢占
# ═══════════════════════════════════════════════════════════

class TestClaimNext:

    async def test_skip_contended_row(self):
        """场景 5：首个候选被并发抢走（rowcount=0）→ 换下一候选成功"""
        claimed = _job(status="running", attempts=1)
        db = _FakeDB([
            _FakeResult(scalars_all=[11, 12]),   # 公平选行候选
            _FakeResult(rowcount=0),             # 抢 11 失败
            _FakeResult(rowcount=1),             # 抢 12 成功
            _select_result(claimed),             # 回读任务行
        ])
        job = await scheduler.claim_next(db, "w1")
        assert job is claimed
        assert len(db.calls) == 4

    async def test_all_contended_returns_none(self):
        """场景 6：候选全部被抢 → None（队列视为空）"""
        db = _FakeDB([
            _FakeResult(scalars_all=[11, 12]),
            _FakeResult(rowcount=0),
            _FakeResult(rowcount=0),
        ])
        assert await scheduler.claim_next(db, "w1") is None


# ═══════════════════════════════════════════════════════════
#  7-9. finish 终态收敛
# ═══════════════════════════════════════════════════════════

class TestFinish:

    async def test_finish_ok(self):
        """场景 7：成功（持有者匹配）→ done；UPDATE 限定 worker 与 running"""
        db = _FakeDB([_FakeResult(rowcount=1)])
        status = await scheduler.finish(db, 1, True, "w1")
        assert status == "done"
        assert "status='done'" in db.calls[0][0]
        assert "worker=:w" in db.calls[0][0]
        assert "status='running'" in db.calls[0][0]

    async def test_finish_fail_retry(self):
        """场景 8：失败且 attempts<max → 回 pending"""
        db = _FakeDB([
            _FakeResult(rowcount=1),            # UPDATE（自己持有）
            _select_result("pending"),          # 回读状态
        ])
        status = await scheduler.finish(db, 1, False, "w1", "embedding failed")
        assert status == "pending"

    async def test_finish_fail_permanent(self):
        """场景 9：失败且 attempts>=max → failed 终态"""
        db = _FakeDB([
            _FakeResult(rowcount=1),
            _select_result("failed"),
        ])
        status = await scheduler.finish(db, 1, False, "w1", "embedding failed")
        assert status == "failed"

    async def test_finish_lost_lease_returns_none(self):
        """场景 10：丢租约（行被回收/接管，UPDATE 0 行）→ None，不写终态"""
        db = _FakeDB([_FakeResult(rowcount=0)])
        assert await scheduler.finish(db, 1, True, "w1") is None
        assert db.committed == 1  # UPDATE 仍显式提交（红线）


# ═══════════════════════════════════════════════════════════
#  11-13. renew_lease 租约三态
# ═══════════════════════════════════════════════════════════

class TestRenewLease:

    async def test_renewed_when_holder_matches(self):
        """场景 11：本进程持有 running 行 → renewed；条件含 worker/status，显式提交"""
        db = _FakeDB([_FakeResult(rowcount=1)])
        assert await scheduler.renew_lease(db, 1, "w1") == scheduler.LEASE_RENEWED
        sql = db.calls[0][0]
        assert "started_at=NOW()" in sql
        assert "status='running'" in sql and "worker=:w" in sql
        assert db.committed == 1

    async def test_finished_when_row_terminal(self):
        """场景 12：行已 done/failed → finished（大概率自己刚收尾，安静退出）"""
        db = _FakeDB([
            _FakeResult(rowcount=0),        # 续约 UPDATE 未命中
            _select_result("done"),         # 回读：已终态
        ])
        assert await scheduler.renew_lease(db, 1, "w1") == scheduler.LEASE_FINISHED

    async def test_lost_when_requeued_or_taken_over(self):
        """场景 13：行回 pending（被回收重投）或被他人接管 running → lost"""
        db = _FakeDB([_FakeResult(rowcount=0), _select_result("pending")])
        assert await scheduler.renew_lease(db, 1, "w1") == scheduler.LEASE_LOST

        db2 = _FakeDB([_FakeResult(rowcount=0), _select_result("running")])
        assert await scheduler.renew_lease(db2, 1, "w1") == scheduler.LEASE_LOST


# ═══════════════════════════════════════════════════════════
#  14-17. ratelimit 退避与闸门降级
# ═══════════════════════════════════════════════════════════

class TestBackoff:

    async def test_retry_then_success(self, monkeypatch):
        """场景 14：前两次限流第三次成功 → 返回结果，共调用 3 次"""
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ratelimit.RetryableError("throttled")
            return "ok"

        assert await ratelimit.call_with_backoff(fn) == "ok"
        assert calls["n"] == 3

    async def test_retry_exhausted(self, monkeypatch):
        """场景 15：一直限流 → 重试耗尽后抛最终错误"""
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))

        async def fn():
            raise ratelimit.RetryableError("throttled")

        with pytest.raises(ratelimit.RetryableError):
            await ratelimit.call_with_backoff(fn)

    async def test_non_retryable_raises_immediately(self, monkeypatch):
        """场景 16：业务异常不重试，立即抛出"""
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            raise ValueError("bad request")

        with pytest.raises(ValueError):
            await ratelimit.call_with_backoff(fn)
        assert calls["n"] == 1

    async def test_gate_degrades_on_redis_failure(self, monkeypatch):
        """场景 17：Redis 异常 → 闸门直通（业务不被阻塞）"""
        async def broken_eval(*a, **kw):
            raise ConnectionError("redis down")

        monkeypatch.setattr(ratelimit.redis, "eval", broken_eval)
        async with ratelimit.Gate(ratelimit.GATE_EMBEDDING):
            pass  # 能进入即代表降级直通成功
