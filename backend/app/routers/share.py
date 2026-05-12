"""
文件管理模块 —— 分享、删除、下载计数、共享广场、转存、图床分享

Redis Key:
  FILE_PUBLIC_ZSET   —— 共享文件有序集合 (score = 下载量)
  FILE_NAME_HASH     —— fileid -> filename 映射
  SHARE_PIC_COUNT_*  —— 用户图床数量
"""
import os
import hashlib
import secrets
from fastapi import APIRouter, Request
from sqlalchemy import select, update, delete
from app.database import Session
from app.models import (
    UserFileList, ShareFileList, FileInfo,
    UserFileCount, UserFileAiDesc,
    SharePictureList,
)
from app.dependencies import check_token
from app.redis_client import redis
from app.minio_client import client, BUCKET

router = APIRouter(prefix="/api", tags=["share"])

# Redis 中使用的 Key 常量
FILE_PUBLIC_ZSET = "FILE_PUBLIC_ZSET"
FILE_NAME_HASH = "FILE_NAME_HASH"
FILE_PUBLIC_COUNT = "FILE_PUBLIC_COUNT"


def _md5_hex(text: str) -> str:
    """计算文本的 MD5 十六进制字符串"""
    return hashlib.md5(text.encode()).hexdigest()


# ============================================================
#  dealfile —— 分享 / 删除 / 下载计数（PV）
# ============================================================

@router.post("/dealfile")
async def dealfile(request: Request, body: dict):
    """文件操作入口：cmd=share 分享, cmd=del 删除, cmd=pv 下载计数"""
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
    elif cmd == "pv":
        return await _pv_file(user, md5_val, filename)
    return {"code": 1}


async def _share_file(user: str, md5_val: str, filename: str) -> dict:
    """分享文件 —— 标记为已分享 + 写入共享列表 + 更新 Redis"""
    fileid = f"{md5_val}{filename}"

    # 先查 Redis ZSET 是否已有该文件（防止重复分享）
    score = await redis.zscore(FILE_PUBLIC_ZSET, fileid)
    if score is not None:
        return {"code": 3}  # 别人已经分享

    async with Session() as db:
        # MySQL 中再确认一次
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

        # 标记为已分享
        ufl.shared_status = 1

        # 插入共享文件列表
        db.add(ShareFileList(user=user, md5=md5_val, file_name=filename))

        # 更新公共文件计数
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == FILE_PUBLIC_COUNT)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=FILE_PUBLIC_COUNT, count=1))

        await db.commit()

    # 更新 Redis：ZSET 添加成员 + HASH 存文件名映射
    await redis.zadd(FILE_PUBLIC_ZSET, {fileid: 0})
    await redis.hset(FILE_NAME_HASH, fileid, filename)

    return {"code": 0}


async def _del_file(user: str, md5_val: str, filename: str) -> dict:
    """
    删除文件 —— 处理分享关联、用户关联、引用计数、AI 记录
    只有当 file_info.count 减到 0 时才物理删除 MinIO 文件
    """
    fileid = f"{md5_val}{filename}"

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

        # 如果已分享，清理分享相关数据
        if shared == 1:
            await db.execute(
                delete(ShareFileList).where(
                    ShareFileList.user == user,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
            )
            # 公共计数 -1
            result = await db.execute(
                select(UserFileCount).where(UserFileCount.user == FILE_PUBLIC_COUNT)
            )
            ufc = result.scalar()
            if ufc and ufc.count > 0:
                ufc.count -= 1
            # Redis 中移除
            await redis.zrem(FILE_PUBLIC_ZSET, fileid)
            await redis.hdel(FILE_NAME_HASH, fileid)

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
        result = await db.execute(select(FileInfo).where(FileInfo.md5 == md5_val))
        fi = result.scalar()
        if fi:
            fi.count -= 1
            # 没有任何用户引用时，物理删除文件
            if fi.count <= 0:
                try:
                    client.remove_object(BUCKET, fi.file_id)
                except Exception:
                    pass
                await db.delete(fi)

        await db.commit()

    # 标记 FAISS 索引脏（下次搜索前自动重建）
    os.makedirs("/tmp/faiss_locks", exist_ok=True)
    dirty_path = f"/tmp/faiss_locks/{_md5_hex(user)}.dirty"
    with open(dirty_path, "w") as f:
        f.write("1")

    return {"code": 0}


async def _pv_file(user: str, md5_val: str, filename: str) -> dict:
    """更新下载计数 —— user_file_list.pv +1, share_file_list.pv +1, Redis ZSET score +1"""
    async with Session() as db:
        # user_file_list 中 pv +1
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

        ufl.pv = (ufl.pv or 0) + 1
        shared = ufl.shared_status

        # 如果已分享，同步更新 share_file_list 和 Redis ZSET
        if shared == 1:
            await db.execute(
                update(ShareFileList)
                .where(
                    ShareFileList.user == user,
                    ShareFileList.md5 == md5_val,
                    ShareFileList.file_name == filename,
                )
                .values(pv=ufl.pv)
            )

        await db.commit()

    if shared == 1:
        fileid = f"{md5_val}{filename}"
        await redis.zincrby(FILE_PUBLIC_ZSET, 1, fileid)

    return {"code": 0}


# ============================================================
#  sharefiles —— 共享广场 / 下载排行
# ============================================================

@router.post("/sharefiles")
async def sharefiles(request: Request, body: dict):
    """共享文件列表：cmd=normal 普通列表, cmd=pvdesc 按下载量降序"""
    cmd = request.query_params.get("cmd", body.get("cmd", "normal"))

    if cmd == "pvdesc":
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
                    "url": fi.url if fi else "",
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
        fileid = f"{md5_val}{filename}"
        await redis.zincrby(FILE_PUBLIC_ZSET, 1, fileid)
        return {"code": 0}

    return {"code": 1}


async def _save_shared(user: str, md5_val: str, filename: str) -> dict:
    """转存共享文件到自己的文件列表"""
    async with Session() as db:
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
        result = await db.execute(select(FileInfo).where(FileInfo.md5 == md5_val))
        fi = result.scalar()
        if fi:
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

        await db.commit()

    return {"code": 0}


async def _cancel_share(user: str, md5_val: str, filename: str) -> dict:
    """取消分享 —— 从 share_file_list 移除 + 恢复 shared_status + Redis 清理"""
    fileid = f"{md5_val}{filename}"

    async with Session() as db:
        # 删除共享记录
        await db.execute(
            delete(ShareFileList).where(
                ShareFileList.user == user,
                ShareFileList.md5 == md5_val,
                ShareFileList.file_name == filename,
            )
        )

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


# ============================================================
#  sharepic —— 图床分享（生成提取码）
# ============================================================

@router.post("/sharepic")
async def sharepic(body: dict):
    """
    图床分享 —— 为图片生成唯一的提取码，存储到 share_picture_list。
    返回 8 位随机数字提取码供用户分享。
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    md5_val = body.get("md5", "")
    filename = body.get("filename", "")
    if not md5_val or not filename:
        return {"code": 1}

    # 图床 URL 的 MD5（用于唯一标识图床链接）
    urlmd5 = hashlib.md5(f"{user}/{md5_val}/{filename}".encode()).hexdigest()

    async with Session() as db:
        # 检查是否已分享过该图床
        result = await db.execute(
            select(SharePictureList).where(
                SharePictureList.user == user,
                SharePictureList.filemd5 == md5_val,
                SharePictureList.urlmd5 == urlmd5,
            )
        )
        existing = result.scalar()
        if existing:
            # 已存在，直接返回原来的提取码
            return {"code": 0, "key": existing.key}

        # 生成 8 位随机数字提取码
        key = "".join([str(secrets.randbelow(10)) for _ in range(8)])

        db.add(SharePictureList(
            user=user,
            filemd5=md5_val,
            file_name=filename,
            urlmd5=urlmd5,
            key=key,
        ))

        # 更新用户图床计数
        count_key = f"SHARE_PIC_COUNT_{user}"
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == count_key)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=count_key, count=1))

        await db.commit()

    return {"code": 0, "key": key}
