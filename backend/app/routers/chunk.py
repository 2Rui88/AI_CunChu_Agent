"""
分片上传模块 —— 支持大文件分片上传和断点续传

流程: chunk_init → chunk_upload (循环) → chunk_merge
Redis Key: chunk:<md5> Hash 存储分片元信息
临时目录: /tmp/chunks/<md5>/ 存储分片数据
"""
import io
import os
import hashlib
from fastapi import APIRouter, Request
from app.dependencies import check_token
from app.redis_client import redis
from app.database import Session
from app.models import FileInfo, UserFileList, UserFileCount
from app.minio_client import client, BUCKET, ensure_bucket

router = APIRouter(prefix="/api", tags=["chunk"])

CHUNK_TEMP_DIR = "/tmp/chunks"


def _get_suffix(filename: str) -> str:
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "unknown"


@router.post("/chunk_init")
async def chunk_init(body: dict):
    """
    分片上传初始化 —— 客户端发送文件元信息，后端在 Redis 和磁盘中创建分片任务。
    如果已存在相同 md5 的任务，返回已上传的分片列表（断点续传）。
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    filename = body.get("filename", "")
    file_md5 = body.get("md5", "")
    filesize = body.get("size", 0)
    chunk_count = body.get("chunkCount", 0)

    redis_key = f"chunk:{file_md5}"

    # 检查是否已存在分片任务（断点续传）
    exists = await redis.exists(redis_key)
    if exists:
        uploaded = await redis.hget(redis_key, "uploaded") or ""
        return {
            "code": 0,
            "chunkCount": int(await redis.hget(redis_key, "chunk_count") or chunk_count),
            "uploadedChunks": uploaded,
        }

    # 新任务：在 Redis 中创建分片元信息
    await redis.hset(redis_key, "filename", filename)
    await redis.hset(redis_key, "filesize", str(filesize))
    await redis.hset(redis_key, "chunk_count", str(chunk_count))
    await redis.hset(redis_key, "user", user)
    await redis.hset(redis_key, "uploaded", "")
    await redis.expire(redis_key, 86400)  # 24小时过期

    # 创建分片临时目录
    chunk_dir = os.path.join(CHUNK_TEMP_DIR, file_md5)
    os.makedirs(chunk_dir, exist_ok=True)

    return {"code": 0, "chunkCount": chunk_count, "uploadedChunks": ""}


@router.post("/chunk_upload")
async def chunk_upload(request: Request):
    """
    分片上传 —— 接收单个分片的二进制数据，保存到本地临时目录。
    更新 Redis 中已上传的分片索引列表。
    """
    # 从 query string 获取参数
    query = request.url.query or ""
    md5_val = ""
    idx = 0
    for part in query.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k == "md5":
                md5_val = v
            elif k == "index":
                idx = int(v)

    if not md5_val:
        return {"code": 1}

    # Token 可选校验
    redis_key = f"chunk:{md5_val}"
    user = await redis.hget(redis_key, "user")
    if not user:
        return {"code": 1}

    # 读取分片二进制数据
    body = await request.body()

    # 保存分片到临时文件
    chunk_dir = os.path.join(CHUNK_TEMP_DIR, md5_val)
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, str(idx))
    with open(chunk_path, "wb") as f:
        f.write(body)

    # 更新 Redis 中已上传分片列表
    uploaded = await redis.hget(redis_key, "uploaded") or ""
    uploaded_list = [x for x in uploaded.split(",") if x]
    if str(idx) not in uploaded_list:
        uploaded_list.append(str(idx))
    await redis.hset(redis_key, "uploaded", ",".join(uploaded_list))

    return {"code": 0}


@router.post("/chunk_merge")
async def chunk_merge(body: dict):
    """
    分片合并 —— 将所有分片合并为完整文件，上传到 MinIO 并写入 MySQL。
    合并前检查 MD5 去重，命中已有文件则跳过物理上传。
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    md5_val = body.get("md5", "")
    filename = body.get("filename", "")
    redis_key = f"chunk:{md5_val}"

    chunk_count_str = await redis.hget(redis_key, "chunk_count")
    if not chunk_count_str:
        return {"code": 1}

    chunk_count = int(chunk_count_str)
    uploaded_str = await redis.hget(redis_key, "uploaded") or ""
    uploaded = [int(x) for x in uploaded_str.split(",") if x]

    # 检查所有分片是否都已上传
    if len(uploaded) != chunk_count:
        return {"code": 1, "msg": f"分片不完整: {len(uploaded)}/{chunk_count}"}

    # 合并分片到完整文件
    chunk_dir = os.path.join(CHUNK_TEMP_DIR, md5_val)
    merged_path = os.path.join(CHUNK_TEMP_DIR, f"{md5_val}_merged")
    with open(merged_path, "wb") as out:
        for i in range(chunk_count):
            chunk_path = os.path.join(chunk_dir, str(i))
            with open(chunk_path, "rb") as f:
                out.write(f.read())

    # 实际 MD5 校验
    actual_md5 = hashlib.md5(open(merged_path, "rb").read()).hexdigest()
    if actual_md5 != md5_val:
        os.remove(merged_path)
        return {"code": 1, "msg": "MD5 校验失败"}

    file_size = os.path.getsize(merged_path)
    suffix = _get_suffix(filename)

    async with Session() as db:
        # MD5 去重：检查是否已有相同文件
        from sqlalchemy import select as _select
        result = await db.execute(_select(FileInfo).where(FileInfo.md5 == md5_val))
        existing = result.scalar()

        if existing:
            existing.count += 1
            url = existing.url
        else:
            ensure_bucket()
            object_name = f"{md5_val[:6]}/{filename}"
            with open(merged_path, "rb") as f:
                client.put_object(BUCKET, object_name, f, file_size)
            url = f"/files/{BUCKET}/{object_name}"
            db.add(FileInfo(
                md5=md5_val,
                file_id=object_name,
                url=url,
                size=file_size,
                type=suffix,
                count=1,
            ))

        # 用户文件关联
        result = await db.execute(
            _select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5_val,
                UserFileList.file_name == filename,
            )
        )
        if result.scalar():
            # 清理临时文件
            os.remove(merged_path)
            import shutil
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return {"code": 5}

        db.add(UserFileList(user=user, md5=md5_val, file_name=filename))

        # 更新用户文件计数
        result = await db.execute(
            _select(UserFileCount).where(UserFileCount.user == user)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=user, count=1))

        await db.commit()

    # 清理 Redis 和临时文件
    await redis.delete(redis_key)
    os.remove(merged_path)
    import shutil
    shutil.rmtree(chunk_dir, ignore_errors=True)

    return {"code": 0}
