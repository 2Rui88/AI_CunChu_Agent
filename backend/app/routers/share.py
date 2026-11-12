"""
文件管理模块 —— 分享、删除、下载计数、共享广场、转存

部门权限隔离：
  - 共享语义收窄为部门内共享：广场列表/排行/转存/计数均只作用于本部门
  - 删除判权：文件所有者本人，或同部门主管（can_manage_file，全系统唯一判权入口）
  - 主管删除他人文件时，连带动作（引用计数/MinIO/FAISS 重建）全部作用在所有者维度

Redis Key（部门化，见 app/share_stats.py）:
  FILE_PUBLIC_ZSET:{dept} —— 部门共享文件有序集合 (score = 下载量，缓存，真值在 MySQL)
  FILE_NAME_HASH:{dept}   —— 部门内 fileid -> filename 映射
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
from app.dependencies import get_actor, can_manage_file
from app.share_stats import (
    bump_public_count, plaza_add, plaza_remove, plaza_incr,
    rebuild_plaza_if_empty, plaza_zset_key, plaza_hash_key,
)
from app.redis_client import redis
from app.minio_client import client, BUCKET, presign_from_db_url
from app.faiss_service import mark_dirty

router = APIRouter(prefix="/api", tags=["share"])


# ============================================================
#  dealfile —— 分享 / 删除
# ============================================================

@router.post("/dealfile")
async def dealfile(request: Request, body: dict):
    """文件操作入口：cmd=share 分享, cmd=del 删除（主管可带 owner 删除部门内他人文件）"""
    user = body.get("user", "")
    token = body.get("token", "")
    actor = await get_actor(user, token)
    if actor is None:
        return {"code": 4}

    # cmd 优先从 URL query string 取，兼容旧系统调用方式
    cmd = request.query_params.get("cmd", body.get("cmd", ""))
    md5_val = body.get("md5", "")
    filename = body.get("filename", "")
    # 删除目标所有者：默认自己；主管从"部门文件"页删除他人文件时显式指定
    owner = body.get("owner", "") or user

    if cmd == "share":
        return await _share_file(actor, md5_val, filename)
    elif cmd == "del":
        return await _del_file(actor, owner, md5_val, filename)
    return {"code": 1}


async def _share_file(actor, md5_val: str, filename: str) -> dict:
    """分享文件（仅自己的文件）—— 标记为已分享 + 写入本部门共享列表 + 更新 Redis"""
    fileid = f"{md5_val}{filename}"

    async with Session() as db:
        # 数据库是真值来源；Redis 可能丢失，不能用于唯一性判断
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == actor.username,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        ufl = result.scalar()
        if ufl is None:
            return {"code": 1}
        if ufl.shared_status == 1:
            return {"code": 3}

        dept_id = ufl.department_id
        # 判重范围 = 本部门（同一文件可在不同部门各自分享）
        result = await db.execute(
            select(ShareFileList).where(
                ShareFileList.department_id == dept_id,
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            ).limit(1)
        )
        if result.scalar() is not None:
            return {"code": 3}

        # 标记为已分享
        ufl.shared_status = 1

        # 插入共享文件列表（部门内共享）
        db.add(ShareFileList(
            user=actor.username, md5=md5_val, file_name=filename, pv=0,
            department_id=dept_id,
        ))

        # 更新部门公共文件计数
        await bump_public_count(db, dept_id, 1)

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return {"code": 3}

    # 更新 Redis：ZSET 添加成员 + HASH 存文件名映射
    await plaza_add(dept_id, fileid, filename)

    return {"code": 0}


async def _del_file(actor, owner: str, md5_val: str, filename: str) -> dict:
    """
    删除文件（自己的文件，或主管删同部门任意成员的文件）
    —— 处理分享关联、用户关联、引用计数、AI 记录。
    所有连带动作（计数/FAISS 重建）作用在**所有者**维度，不是操作者。
    只有当 file_info.count 减到 0 时才物理删除 MinIO 文件。
    MinIO 删除放在事务提交之后：提交前删对象而 commit 失败会反过来弄丢
    仍在被引用的文件数据；提交后删失败仅留下可排查的孤儿对象（记日志）。
    """
    fileid = f"{md5_val}{filename}"
    object_to_remove = None

    async with Session() as db:
        # 按所有者定位文件行（主管删他人文件时 owner ≠ 操作者）
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == owner,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        ufl = result.scalar()
        if ufl is None:
            return {"code": 1}

        # 判权：所有者本人，或同部门主管
        if not can_manage_file(actor, owner, ufl.department_id):
            return {"code": 6, "msg": "无权操作他人的文件"}

        dept_id = ufl.department_id
        shared = ufl.shared_status

        # 如果已分享，清理分享关联数据（Redis 部分推迟到 commit 之后）
        if shared == 1:
            delete_result = await db.execute(
                delete(ShareFileList).where(
                    ShareFileList.user == owner,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
            )
            if delete_result.rowcount != 1:
                await db.rollback()
                return {"code": 1}
            # 部门公共计数 -1
            await bump_public_count(db, dept_id, -1)

        # 删除用户 AI 描述记录（所有者维度）
        await db.execute(
            delete(UserFileAiDesc).where(
                UserFileAiDesc.user == owner, UserFileAiDesc.md5 == md5_val
            )
        )

        # 删除用户文件关联
        await db.delete(ufl)

        # 用户文件计数 -1（所有者维度）
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == owner)
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
        await plaza_remove(dept_id, fileid)
    if object_to_remove:
        try:
            await asyncio.to_thread(client.remove_object, BUCKET, object_to_remove)
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "MinIO 对象删除失败（孤儿对象残留）bucket=%s object=%s",
                BUCKET, object_to_remove,
            )

    # 标记 FAISS 需要重建（下次搜索自动触发）—— 所有者维度
    mark_dirty(owner)

    return {"code": 0}


# ============================================================
#  sharefiles —— 共享广场 / 下载排行（部门内，需登录）
# ============================================================

@router.post("/sharefiles")
async def sharefiles(request: Request, body: dict):
    """共享文件列表（仅本部门）：cmd=normal 普通列表, cmd=pvdesc 按下载量降序"""
    user = body.get("user", "")
    token = body.get("token", "")
    actor = await get_actor(user, token)
    if actor is None:
        return {"code": 4}
    dept_id = actor.department_id

    cmd = request.query_params.get("cmd", body.get("cmd", "normal"))

    if cmd == "pvdesc":
        # 排行榜以 MySQL 为真值来源：Redis 丢失时懒重建
        await rebuild_plaza_if_empty(dept_id)
        # 从本部门 Redis ZSET 获取下载排行
        zset, hash_key = plaza_zset_key(dept_id), plaza_hash_key(dept_id)
        fileids = await redis.zrevrange(zset, 0, 9)
        files = []
        for fid in fileids:
            filename = await redis.hget(hash_key, fid) or ""
            score = await redis.zscore(zset, fid)
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
                .where(ShareFileList.department_id == dept_id)
                .order_by(ShareFileList.create_time.desc())
                .limit(100)
            )
            rows = result.scalars().all()
            files = []
            for r in rows:
                # 联查 file_info 获取 url / 大小 / 类型
                fi_result = await db.execute(
                    select(FileInfo).where(FileInfo.md5 == r.md5).limit(1)
                )
                fi = fi_result.scalar()
                files.append({
                    "user": r.user,
                    "md5": r.md5,
                    "file_name": r.file_name,
                    "pv": r.pv,
                    "size": fi.size if fi else 0,
                    "type": (fi.type or "") if fi else "",
                    "url": presign_from_db_url(fi.url) if fi and fi.url else "",
                    "create_time": str(r.create_time),
                })
            return {"code": 0, "files": files}

    return {"code": 1}


# ============================================================
#  dealsharefile —— 转存 / 取消分享 / 下载计数（部门内）
# ============================================================

@router.post("/dealsharefile")
async def dealsharefile(request: Request, body: dict):
    """共享文件操作：cmd=save 转存, cmd=cancel 取消分享, cmd=pv 下载计数（均限本部门）"""
    user = body.get("user", "")
    token = body.get("token", "")
    actor = await get_actor(user, token)
    if actor is None:
        return {"code": 4}

    cmd = request.query_params.get("cmd", body.get("cmd", ""))
    md5_val = body.get("md5", "")
    filename = body.get("filename", "")

    if cmd == "save":
        return await _save_shared(actor, md5_val, filename)
    elif cmd == "cancel":
        return await _cancel_share(actor, md5_val, filename)
    elif cmd == "pv":
        # 下载计数：MySQL（真值）与 Redis（排行榜缓存）同步自增。
        # 先确认文件确已在**本部门**共享，防止对任意 md5 的 pv 请求
        # 在 ZSET 里制造幽灵条目、或给别的部门刷下载量
        async with Session() as db:
            shared = await db.execute(
                select(ShareFileList).where(
                    ShareFileList.department_id == actor.department_id,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                ).limit(1)
            )
            if not shared.scalar():
                return {"code": 1, "msg": "file not shared"}
            await db.execute(
                update(ShareFileList)
                .where(
                    ShareFileList.department_id == actor.department_id,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
                .values(pv=ShareFileList.pv + 1)
            )
            await db.commit()
        await plaza_incr(actor.department_id, f"{md5_val}{filename}")
        return {"code": 0}

    return {"code": 1}


async def _save_shared(actor, md5_val: str, filename: str) -> dict:
    """转存本部门共享文件到自己的文件列表（部门随转存者）"""
    async with Session() as db:
        # 必须确实处于本部门共享状态，防止凭 md5+文件名转存他人/外部门私有文件
        shared = await db.execute(
            select(ShareFileList).where(
                ShareFileList.department_id == actor.department_id,
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            ).limit(1)
        )
        if not shared.scalar():
            return {"code": 1, "msg": "file not shared"}

        # 检查是否已经拥有
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == actor.username,
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

        db.add(UserFileList(
            user=actor.username, md5=md5_val, file_name=filename,
            department_id=actor.department_id,
        ))

        # 用户文件计数 +1
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == actor.username)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=actor.username, count=1))

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return {"code": 5}

    return {"code": 0}


async def _cancel_share(actor, md5_val: str, filename: str) -> dict:
    """取消分享（仅自己的文件）—— 从 share_file_list 移除 + 恢复 shared_status + Redis 清理"""
    fileid = f"{md5_val}{filename}"

    async with Session() as db:
        owned = await db.execute(
            select(UserFileList).where(
                UserFileList.user == actor.username,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
                UserFileList.shared_status == 1,
            ).limit(1)
        )
        ufl = owned.scalar()
        if ufl is None:
            return {"code": 1}
        dept_id = ufl.department_id

        # 删除共享记录
        delete_result = await db.execute(
            delete(ShareFileList).where(
                ShareFileList.user == actor.username,
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
                UserFileList.user == actor.username,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
            .values(shared_status=0)
        )

        # 部门公共计数 -1
        await bump_public_count(db, dept_id, -1)

        await db.commit()

    # Redis 清理
    await plaza_remove(dept_id, fileid)

    return {"code": 0}
