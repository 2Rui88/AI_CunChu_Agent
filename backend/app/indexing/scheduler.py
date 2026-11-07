"""
任务调度器 —— 投递 / 原子领取（公平） / 超时回收 / 终态收敛 / 状态查询

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


async def finish(db, job_id: int, ok: bool, error: str = "") -> str:
    """
    收敛终态：成功 → done；失败且 attempts < max → 回 pending（自动重试）；
    失败且 attempts >= max → failed。返回最终状态。
    """
    err = (error or "")[:500]
    if ok:
        await db.execute(
            text("UPDATE indexing_jobs SET status='done', error='', worker='', "
                 "finished_at=NOW() WHERE id=:id"),
            {"id": job_id},
        )
        await db.commit()
        return "done"
    result = await db.execute(
        text("UPDATE indexing_jobs SET status=IF(attempts < :maxa, 'pending', 'failed'), "
             "error=:err, worker='', "
             "finished_at=IF(attempts >= :maxa, NOW(), finished_at) "
             "WHERE id=:id"),
        {"maxa": settings.indexing_max_attempts, "err": err, "id": job_id},
    )
    await db.commit()
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
