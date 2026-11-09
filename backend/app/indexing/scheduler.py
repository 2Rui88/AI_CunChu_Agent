"""
任务调度器 —— 投递 / 原子领取（公平） / 超时回收 / 租约续约 / 终态收敛 / 状态与统计

只操作 indexing_jobs 表本身，不执行任务（执行见 pipeline.run_indexing_job）。
多消费者安全：领取是「公平选行 + 条件 UPDATE 抢占」，两个消费者同抢一行时
只有一个 UPDATE 生效，落空方自动换下一行。

公平规则：优先领取「当前在跑任务最少的用户」的 pending（同用户内按 id FIFO），
防单个大用户的大文件队列饿死其他用户。
"""
import logging

from sqlalchemy import select, text

from app.config import settings
from app.database import Session
from app.indexing.models import IndexingJob
from app.models import UserFileAiDesc

logger = logging.getLogger("uvicorn.error")

# 入队结果（供路由直接透传）
QUEUED = "queued"          # 已投递（新任务或复用在途任务）
ALREADY_DONE = "done"      # 非 force 且用户侧已有完成索引，无需任务
REQUEUED = "requeued"      # failed/done 行被重置重新入队（手动重建）

# 租约续约三态（worker 心跳据此分支）
LEASE_RENEWED = "renewed"    # 续约成功，任务仍归本进程持有
LEASE_FINISHED = "finished"  # 行已收敛终态（大概率是自己刚收尾）——安静退出
LEASE_LOST = "lost"          # 行已被回收重投/被他人接管——调用方应止损

# 公平选行：该用户 running 数最少的 pending 优先，同优先级按 id FIFO
_PICK_SQL = text(
    "SELECT j.id FROM indexing_jobs j "
    "LEFT JOIN (SELECT user, COUNT(*) c FROM indexing_jobs "
    "           WHERE status='running' GROUP BY user) r ON r.user = j.user "
    "WHERE j.status='pending' "
    "ORDER BY COALESCE(r.c, 0), j.id LIMIT 3"
)


async def enqueue(db, *, md5: str, user: str, filename: str, file_type: str,
                  force: bool = False) -> tuple[str, dict]:
    """
    投递/复用一个文件的任务，返回 (结果, 状态视图)。幂等：
    - pending/running 中 → 复用（QUEUED，不重复建行、不改触发者）
    - done 且非 force → ALREADY_DONE（不投递）
    - done 且 force / failed → 重置入队（attempts 归零）
    另有用户级短路：非 force 且用户已有完成切片记录 → ALREADY_DONE（免投递）。
    """
    if not force:
        row = (await db.execute(
            select(UserFileAiDesc.id).where(
                UserFileAiDesc.user == user,
                UserFileAiDesc.md5 == md5,
                UserFileAiDesc.chunk_index == 0,
                UserFileAiDesc.status == 1,
            ).limit(1)
        )).scalar()
        if row:
            return ALREADY_DONE, {"md5": md5, "status": "done", "attempts": 0, "error": ""}

    job = (await db.execute(
        select(IndexingJob).where(IndexingJob.md5 == md5).limit(1)
    )).scalar()

    if job is None:
        # 显式赋初值：不依赖 DB default（新建对象 commit 后不会自动回读，
        # public_dict 需立即给出正确状态视图）
        job = IndexingJob(md5=md5, user=user, filename=filename or "",
                          file_type=(file_type or "").lower(),
                          force=1 if force else 0,
                          status="pending", attempts=0)
        db.add(job)
        await db.commit()
        return QUEUED, job.public_dict()

    if job.status in ("pending", "running"):
        return QUEUED, job.public_dict()

    # done(需 force) / failed：重置入队，attempts 归零，更新为本次触发者
    job.user = user
    job.filename = filename or job.filename
    job.file_type = (file_type or job.file_type).lower()
    job.force = 1 if force else 0
    job.status = "pending"
    job.attempts = 0
    job.error = ""
    job.worker = ""
    job.started_at = None
    job.finished_at = None
    await db.commit()
    return REQUEUED, job.public_dict()


async def claim_next(db, worker_id: str) -> IndexingJob | None:
    """公平领取一个 pending：条件 UPDATE 抢占，被抢走则换下一行（最多 3 个候选）。

    UPDATE 后必须显式 commit——async with Session() 退出只 close（回滚未提交
    事务）；不提交的话 DB 里任务仍是 pending，每个进程每轮都会重复领取，
    造成多进程并发执行同一任务（唯一键冲突 / 重复消耗 API 调用）。
    """
    candidate_ids = (await db.execute(_PICK_SQL)).scalars().all()
    for jid in candidate_ids:
        result = await db.execute(
            text(
                "UPDATE indexing_jobs SET status='running', started_at=NOW(), "
                "worker=:w, attempts=attempts+1, error='' WHERE id=:id AND status='pending'"
            ),
            {"w": worker_id, "id": jid},
        )
        if result.rowcount == 1:
            await db.commit()  # 抢占立即提交生效，其他消费者立刻不可见
            return (await db.execute(
                select(IndexingJob).where(IndexingJob.id == jid)
            )).scalar()
    return None


async def renew_lease(db, job_id: int, worker: str) -> str:
    """
    任务租约续期（消费者心跳调用）：把 started_at 推到当前时刻。

    心跳证明的是「本进程（事件循环）仍活着」，不感知任务进度；执行/心跳
    共享事件循环、同生共死。条件限定 status='running' 且 worker 匹配：
    只有当前持有者的续约才生效。返回三态——
    - renewed：续约成功；
    - finished：行已 done/failed（大概率是自己的执行协程刚收尾），
      调用方应安静退出，不得触发取消；
    - lost：行已回 pending（被超时回收重投）或被其他 worker 接管，
      调用方应取消执行止损（旧执行者继续跑只会重复消耗 API）。
    """
    result = await db.execute(
        text("UPDATE indexing_jobs SET started_at=NOW() "
             "WHERE id=:id AND status='running' AND worker=:w"),
        {"id": job_id, "w": worker},
    )
    await db.commit()  # 抢占/续约/终态类 UPDATE 后必须显式提交
    if result.rowcount == 1:
        return LEASE_RENEWED
    status = (await db.execute(
        select(IndexingJob.status).where(IndexingJob.id == job_id)
    )).scalar()
    return LEASE_FINISHED if status in ("done", "failed") else LEASE_LOST


async def reclaim_stale(db) -> int:
    """
    回收超时 running（消费者崩溃/被杀）：attempts 未超上限 → 回 pending 重投；
    已超上限 → failed（可观察终态，等待手动重建）。返回回收行数。

    超时判定必须用数据库侧 NOW()：started_at 由 claim 时的 MySQL NOW() 写入，
    若在 Python 侧生成 UTC 时间比较（MySQL 会话时区为 +08:00），会把刚领取
    的任务全部误判为超时，引发多进程重复执行同一任务。
    """
    result = await db.execute(
        text(
            "UPDATE indexing_jobs SET status=IF(attempts < :maxa, 'pending', 'failed'), "
            "error='job timed out', worker='', "
            "finished_at=IF(attempts >= :maxa, NOW(), finished_at) "
            "WHERE status='running' AND started_at < NOW() - INTERVAL :timeout SECOND"
        ),
        {"maxa": settings.indexing_max_attempts,
         "timeout": settings.indexing_job_timeout_seconds},
    )
    await db.commit()
    if result.rowcount:
        logger.warning("indexing: reclaimed %d stale running job(s)", result.rowcount)
    return result.rowcount


async def finish(db, job_id: int, ok: bool, worker: str, error: str = "") -> str | None:
    """
    收敛终态（仅持有者可写）：成功 → done；失败且 attempts < max → 回 pending
    （自动重试）；失败且 attempts >= max → failed。返回最终状态。

    WHERE 限定 status='running' 且 worker 匹配：丢租约的旧执行者（行已被
    超时回收重投或被他人接管）影响 0 行，返回 None——旧执行者既不踩踏
    新持有者的行、也不写终态（该次执行作废，由新持有者/重试收敛）。
    """
    err = (error or "")[:500]
    if ok:
        result = await db.execute(
            text("UPDATE indexing_jobs SET status='done', error='', worker='', "
                 "finished_at=NOW() "
                 "WHERE id=:id AND status='running' AND worker=:w"),
            {"id": job_id, "w": worker},
        )
    else:
        result = await db.execute(
            text("UPDATE indexing_jobs SET status=IF(attempts < :maxa, 'pending', 'failed'), "
                 "error=:err, worker='', "
                 "finished_at=IF(attempts >= :maxa, NOW(), finished_at) "
                 "WHERE id=:id AND status='running' AND worker=:w"),
            {"maxa": settings.indexing_max_attempts, "err": err,
             "id": job_id, "w": worker},
        )
    await db.commit()
    if result.rowcount == 0:
        return None  # 丢租约：行不归自己，不写终态
    if ok:
        return "done"
    # 回读最终状态（pending=会重试 / failed=终态）
    job = (await db.execute(
        select(IndexingJob.status).where(IndexingJob.id == job_id)
    )).scalar()
    return job or "failed"


async def get_status(db, *, user: str, md5: str) -> dict | None:
    """查询任务状态（调用方负责鉴权：仅任务触发者可查）"""
    job = (await db.execute(
        select(IndexingJob).where(IndexingJob.md5 == md5).limit(1)
    )).scalar()
    if job is None or job.user != user:
        return None
    return job.public_dict()


async def queue_stats(db) -> dict:
    """
    队列全局聚合统计（监控用，不含任何用户名）：
    各 status 计数 / 最老 pending 等待秒数 / running 行的去重 worker 数。

    时间一律用数据库侧 NOW()/TIMESTAMPDIFF 计算（MySQL 会话时区 +08:00，
    Python 侧时间与之比较会把刚入队的任务算成超时）。
    判定口径：最老 pending 年龄持续增长 && running==0 ⇒ 没有活着的消费者
    （容器 up 但没人消费：开关配错/循环卡死，Docker 健康检查看不见）。
    """
    counts = dict((await db.execute(
        text("SELECT status, COUNT(*) FROM indexing_jobs GROUP BY status")
    )).all())
    oldest = (await db.execute(
        text("SELECT TIMESTAMPDIFF(SECOND, MIN(create_time), NOW()) "
             "FROM indexing_jobs WHERE status='pending'")
    )).scalar()
    workers = (await db.execute(
        text("SELECT COUNT(DISTINCT worker) FROM indexing_jobs "
             "WHERE status='running' AND worker <> ''")
    )).scalar()
    return {
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "oldest_pending_seconds": int(oldest or 0),
        "active_workers": int(workers or 0),
    }
