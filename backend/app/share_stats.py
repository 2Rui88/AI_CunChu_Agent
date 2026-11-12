"""
部门化共享基础设施 —— 公共计数哨兵行 + 共享广场排行榜 Redis 结构

共享语义按部门隔离后，原全局的三个结构全部带上部门维度：
  user_file_count 哨兵行 FILE_PUBLIC_COUNT  → FILE_PUBLIC_COUNT:{dept_id}（每部门一行）
  Redis ZSET      FILE_PUBLIC_ZSET          → FILE_PUBLIC_ZSET:{dept_id}
  Redis HASH      FILE_NAME_HASH            → FILE_NAME_HASH:{dept_id}

share.py（HTTP 通道）与 agent_tools.py（Agent 工具通道）共用本模块，
避免两条平行实现各自维护一份部门化逻辑而漂移。
"""
from sqlalchemy import select

from app.database import Session
from app.models import ShareFileList, UserFileCount
from app.redis_client import redis


def public_count_user(dept_id: int) -> str:
    """部门公共共享计数哨兵行（user_file_count.user 的值）"""
    return f"FILE_PUBLIC_COUNT:{dept_id}"


def plaza_zset_key(dept_id: int) -> str:
    """部门下载排行榜 ZSET key"""
    return f"FILE_PUBLIC_ZSET:{dept_id}"


def plaza_hash_key(dept_id: int) -> str:
    """部门 fileid -> filename 映射 HASH key"""
    return f"FILE_NAME_HASH:{dept_id}"


async def bump_public_count(db, dept_id: int, delta: int) -> None:
    """
    部门公共共享计数 ±delta。+1 时缺行则建；-1 时缺行/归零下方不减（与历史行为一致）。
    在调用方事务内执行，随其 commit/rollback。
    """
    key = public_count_user(dept_id)
    result = await db.execute(select(UserFileCount).where(UserFileCount.user == key))
    ufc = result.scalar()
    if ufc:
        if delta > 0 or ufc.count > 0:
            ufc.count += delta
    elif delta > 0:
        db.add(UserFileCount(user=key, count=delta))


async def plaza_add(dept_id: int, fileid: str, file_name: str) -> None:
    """新增共享：ZSET 加成员（score=0）+ HASH 记文件名映射"""
    try:
        await redis.zadd(plaza_zset_key(dept_id), {fileid: 0})
        await redis.hset(plaza_hash_key(dept_id), fileid, file_name)
    except Exception:
        pass  # Redis 是缓存，失败由懒重建兜底


async def plaza_remove(dept_id: int, fileid: str) -> None:
    """取消共享/删除共享文件：ZSET 与 HASH 清理"""
    try:
        await redis.zrem(plaza_zset_key(dept_id), fileid)
        await redis.hdel(plaza_hash_key(dept_id), fileid)
    except Exception:
        pass


async def plaza_incr(dept_id: int, fileid: str) -> None:
    """共享广场下载计数 +1（真值在 MySQL，此处只同步排行榜缓存）"""
    try:
        await redis.zincrby(plaza_zset_key(dept_id), 1, fileid)
    except Exception:
        pass


async def rebuild_plaza_if_empty(dept_id: int) -> None:
    """部门排行榜丢失（清空/重启丢数据）时，从 MySQL 真值懒重建"""
    try:
        zset = plaza_zset_key(dept_id)
        if await redis.zcard(zset) > 0:
            return
        async with Session() as db:
            rows = (await db.execute(
                select(ShareFileList)
                .where(ShareFileList.department_id == dept_id)
                .order_by(ShareFileList.pv.desc())
                .limit(500)
            )).scalars().all()
        if not rows:
            return
        hash_key = plaza_hash_key(dept_id)
        pipe = redis.pipeline()
        for r in rows:
            fileid = f"{r.md5}{r.file_name}"
            pipe.zadd(zset, {fileid: r.pv or 0})
            pipe.hset(hash_key, fileid, r.file_name)
        await pipe.execute()
    except Exception:
        pass  # 重建失败不影响本次返回（zset 为空 → 空列表）
