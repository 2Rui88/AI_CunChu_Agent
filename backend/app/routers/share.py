"""
文件管理模块 —— 分享、删除、下载计数、共享广场、转存

Redis Key:
  FILE_PUBLIC_ZSET   —— 共享文件有序集合 (score = 下载量，缓存，真值在 MySQL)
  FILE_NAME_HASH     —— fileid -> filename 映射
"""
import asyncio
import logging
from fastapi import APIRouter, Request
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from app.database import Session
from app.models import (
    UserFileList, ShareFileList, FileInfo,
    UserFileCount, UserFileAiDesc,
)
from app.dependencies import check_token
from app.redis_client import redis
from app.minio_client import client, BUCKET, presign_from_db_url
from app.faiss_service import mark_dirty

router = APIRouter(prefix="/api", tags=["share"])

# Redis 中使用的 Key 常量
FILE_PUBLIC_ZSET = "FILE_PUBLIC_ZSET"
FILE_NAME_HASH = "FILE_NAME_HASH"
FILE_PUBLIC_COUNT = "FILE_PUBLIC_COUNT"


# ============================================================
#  dealfile —— 分享 / 删除
# ============================================================

@router.post("/dealfile")
async def dealfile(request: Request, body: dict):
    """文件操作入口：cmd=share 分享, cmd=del 删除"""
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    # cmd 优先从 URL query string 取，兼容旧系统调用方式
    cmd = request.query_params.get("cmd", body.get("cmd", ""))
    md5_val = body.get("md5", "")
    filename = body.get("filename", "")

    if cmd == "share":
        return await _share_file(user, md5_val, filename)
    elif cmd == "del":
        return await _del_file(user, md5_val, filename)
    return {"code": 1}


async def _share_file(user: str, md5_val: str, filename: str) -> dict:
    """分享文件 —— 标记为已分享 + 写入共享列表 + 更新 Redis"""
    fileid = f"{md5_val}{filename}"

    async with Session() as db:
        # 数据库是真值来源；Redis 可能丢失，不能用于唯一性判断
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        ufl = result.scalar()
        if ufl is None:
            return {"code": 1}
        if ufl.shared_status == 1:
            return {"code": 3}

        result = await db.execute(
            select(ShareFileList).where(
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            ).limit(1)
        )
        if result.scalar() is not None:
            return {"code": 3}

        # 标记为已分享
        ufl.shared_status = 1

        # 插入共享文件列表
        db.add(ShareFileList(user=user, md5=md5_val, file_name=filename, pv=0))

        # 更新公共文件计数
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == FILE_PUBLIC_COUNT)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=FILE_PUBLIC_COUNT, count=1))

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return {"code": 3}

    # 更新 Redis：ZSET 添加成员 + HASH 存文件名映射
    await redis.zadd(FILE_PUBLIC_ZSET, {fileid: 0})
    await redis.hset(FILE_NAME_HASH, fileid, filename)

    return {"code": 0}


async def _del_file(user: str, md5_val: str, filename: str) -> dict:
    """
    删除文件 —— 处理分享关联、用户关联、引用计数、AI 记录
    只有当 file_info.count 减到 0 时才物理删除 MinIO 文件。
    MinIO 删除放在事务提交之后：提交前删对象而 commit 失败会反过来弄丢
    仍在被引用的文件数据；提交后删失败仅留下可排查的孤儿对象（记日志）。
    """
    fileid = f"{md5_val}{filename}"
    object_to_remove = None

    async with Session() as db:
        # 查询用户文件关联
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        ufl = result.scalar()
        if ufl is None:
            return {"code": 1}

        shared = ufl.shared_status

        # 如果已分享，清理分享关联数据（Redis 部分推迟到 commit 之后）
        if shared == 1:
            delete_result = await db.execute(
                delete(ShareFileList).where(
                    ShareFileList.user == user,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
            )
            if delete_result.rowcount != 1:
                await db.rollback()
                return {"code": 1}
            # 公共计数 -1
            result = await db.execute(
                select(UserFileCount).where(UserFileCount.user == FILE_PUBLIC_COUNT)
            )
            ufc = result.scalar()
            if ufc and ufc.count > 0:
                ufc.count -= 1

        # 删除用户 AI 描述记录
        await db.execute(
            delete(UserFileAiDesc).where(
                UserFileAiDesc.user == user, UserFileAiDesc.md5 == md5_val
            )
        )

        # 删除用户文件关联
        await db.delete(ufl)

        # 用户文件计数 -1
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == user)
        )
        ufc = result.scalar()
        if ufc and ufc.count > 0:
            ufc.count -= 1

        # file_info 引用计数 -1
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).with_for_update()
        )
        fi = result.scalar()
        if fi:
            fi.count -= 1
            # 没有任何用户引用时，物理删除文件（对象本身推迟到 commit 后）
            if fi.count <= 0:
                object_to_remove = fi.file_id
                await db.delete(fi)

        await db.commit()

    # 提交成功后的收尾：Redis 清理（失败由懒重建兜底）+ MinIO 对象删除
    if shared == 1:
        try:
            await redis.zrem(FILE_PUBLIC_ZSET, fileid)
            await redis.hdel(FILE_NAME_HASH, fileid)
        except Exception:
            pass
    if object_to_remove:
        try:
            await asyncio.to_thread(client.remove_object, BUCKET, object_to_remove)
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "MinIO 对象删除失败（孤儿对象残留）bucket=%s object=%s",
                BUCKET, object_to_remove,
            )

    # 标记 FAISS 需要重建（下次搜索自动触发）
    mark_dirty(user)

    return {"code": 0}


# ============================================================
#  sharefiles —— 共享广场 / 下载排行
# ============================================================

async def _rebuild_public_zset_if_empty():
    """Redis 排行榜丢失（清空/重启丢数据）时，从 MySQL 真值懒重建"""
    try:
        if await redis.zcard(FILE_PUBLIC_ZSET) > 0:
            return
        async with Session() as db:
            rows = (await db.execute(
                select(ShareFileList).order_by(ShareFileList.pv.desc()).limit(500)
            )).scalars().all()
        if not rows:
            return
        pipe = redis.pipeline()
        for r in rows:
            fileid = f"{r.md5}{r.file_name}"
            pipe.zadd(FILE_PUBLIC_ZSET, {fileid: r.pv or 0})
            pipe.hset(FILE_NAME_HASH, fileid, r.file_name)
        await pipe.execute()
    except Exception:
        pass  # 重建失败不影响本次返回（zset 为空 → 空列表）


@router.post("/sharefiles")
async def sharefiles(request: Request, body: dict):
    """共享文件列表：cmd=normal 普通列表, cmd=pvdesc 按下载量降序"""
    cmd = request.query_params.get("cmd", body.get("cmd", "normal"))

    if cmd == "pvdesc":
        # 排行榜以 MySQL 为真值来源：Redis 丢失时懒重建
        await _rebuild_public_zset_if_empty()
        # 从 Redis ZSET 获取下载排行
        fileids = await redis.zrevrange(FILE_PUBLIC_ZSET, 0, 9)
        files = []
        for fid in fileids:
            filename = await redis.hget(FILE_NAME_HASH, fid) or ""
            score = await redis.zscore(FILE_PUBLIC_ZSET, fid)
            files.append({
                "fileid": fid,
                "file_name": filename,
                "pv": int(score or 0),
            })
        return {"code": 0, "files": files}

    if cmd == "normal":
        async with Session() as db:
            result = await db.execute(
                select(ShareFileList)
                .order_by(ShareFileList.create_time.desc())
                .limit(100)
            )
            rows = result.scalars().all()
            files = []
            for r in rows:
                # 联查 file_info 获取 url
                fi_result = await db.execute(
                    select(FileInfo).where(FileInfo.md5 == r.md5).limit(1)
                )
                fi = fi_result.scalar()
                files.append({
                    "user": r.user,
                    "md5": r.md5,
                    "file_name": r.file_name,
                    "pv": r.pv,
                    "url": presign_from_db_url(fi.url) if fi and fi.url else "",
                    "create_time": str(r.create_time),
                })
            return {"code": 0, "files": files}

    return {"code": 1}


# ============================================================
#  dealsharefile —— 转存 / 取消分享 / 下载计数
# ============================================================

@router.post("/dealsharefile")
async def dealsharefile(request: Request, body: dict):
    """共享文件操作：cmd=save 转存, cmd=cancel 取消分享, cmd=pv 下载计数"""
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    cmd = request.query_params.get("cmd", body.get("cmd", ""))
    md5_val = body.get("md5", "")
    filename = body.get("filename", "")

    if cmd == "save":
        return await _save_shared(user, md5_val, filename)
    elif cmd == "cancel":
        return await _cancel_share(user, md5_val, filename)
    elif cmd == "pv":
        # 下载计数：MySQL（真值）与 Redis（排行榜缓存）同步自增。
        # 先确认文件确已共享，防止对任意 md5 的 pv 请求在 ZSET 里制造幽灵条目
        async with Session() as db:
            shared = await db.execute(
                select(ShareFileList).where(
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                ).limit(1)
            )
            if not shared.scalar():
                return {"code": 1, "msg": "file not shared"}
            await db.execute(
                update(ShareFileList)
                .where(
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
                .values(pv=ShareFileList.pv + 1)
            )
            await db.commit()
        await redis.zincrby(FILE_PUBLIC_ZSET, 1, f"{md5_val}{filename}")
        return {"code": 0}

    return {"code": 1}


async def _save_shared(user: str, md5_val: str, filename: str) -> dict:
    """转存共享文件到自己的文件列表（仅限确已出现在共享广场的文件）"""
    async with Session() as db:
        # 必须确实处于共享状态，防止凭 md5+文件名转存他人私有文件
        shared = await db.execute(
            select(ShareFileList).where(
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            ).limit(1)
        )
        if not shared.scalar():
            return {"code": 1, "msg": "file not shared"}

        # 检查是否已经拥有
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        if result.scalar():
            return {"code": 5}

        # file_info 引用计数 +1
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).with_for_update()
        )
        fi = result.scalar()
        if fi is None:
            return {"code": 1, "msg": "file not found"}
        fi.count += 1

        db.add(UserFileList(user=user, md5=md5_val, file_name=filename))

        # 用户文件计数 +1
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == user)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=user, count=1))

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return {"code": 5}

    return {"code": 0}


async def _cancel_share(user: str, md5_val: str, filename: str) -> dict:
    """取消分享 —— 从 share_file_list 移除 + 恢复 shared_status + Redis 清理"""
    fileid = f"{md5_val}{filename}"

    async with Session() as db:
        owned = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
                UserFileList.shared_status == 1,
            ).limit(1)
        )
        if owned.scalar() is None:
            return {"code": 1}

        # 删除共享记录
        delete_result = await db.execute(
            delete(ShareFileList).where(
                ShareFileList.user == user,
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            )
        )
        if delete_result.rowcount != 1:
            await db.rollback()
            return {"code": 1}

        # 恢复用户文件 shared_status
        await db.execute(
            update(UserFileList)
            .where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
            .values(shared_status=0)
        )

        # 公共计数 -1
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == FILE_PUBLIC_COUNT)
        )
        ufc = result.scalar()
        if ufc and ufc.count > 0:
            ufc.count -= 1

        await db.commit()

    # Redis 清理
    await redis.zrem(FILE_PUBLIC_ZSET, fileid)
    await redis.hdel(FILE_NAME_HASH, fileid)

    return {"code": 0}
