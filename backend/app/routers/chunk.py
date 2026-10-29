"""
分片上传模块 —— 支持大文件分片上传和断点续传

流程: chunk_init → chunk_upload (循环) → chunk_merge
Redis Key: chunk:<user>:<md5> Hash 存储分片元信息（按用户隔离）
临时目录: /data/chunks/<用户名哈希>/<md5>/ 存储分片数据
"""
import os
import re
import time
import shutil
import asyncio
import hashlib
from urllib.parse import parse_qs
from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError
from app.dependencies import check_token
from app.file_ops import attach_file_to_user
from app.redis_client import redis
from app.database import Session
from app.models import FileInfo, UserFileList, UserFileCount
from app.minio_client import client, BUCKET, ensure_bucket

router = APIRouter(prefix="/api", tags=["chunk"])

# 挂载在 docker 卷（chunk_data:/data/chunks）上，容器重启后分片仍可用于断点续传
CHUNK_TEMP_DIR = "/data/chunks"

# 与 Redis 任务 TTL 对齐：超过该时长没动静的分片目录必然已无任务元数据
# （merge 不可能成功），属可回收垃圾
CHUNK_STALE_SECONDS = 86400

# 合法 md5 为 32 位十六进制；同时杜绝 "../" 之类的路径穿越
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_MAX_CHUNK_COUNT = 1000000
# 单个分片大小上限：前端 CHUNK_SIZE(10MB) + 传输余量，防止匿名灌盘
_MAX_CHUNK_BODY = 12 * 1024 * 1024

# 原子地把分片序号追加进 uploaded 列表（HGET→拼接→HSET 的读改写竞态会让
# 后写覆盖先写：某分片文件已在盘但列表缺失，merge 误报不完整）
_ADD_UPLOADED_LUA = """
local cur = redis.call('HGET', KEYS[1], 'uploaded') or ''
local idx = ARGV[1]
if cur == '' then
  redis.call('HSET', KEYS[1], 'uploaded', idx)
  return 1
end
for p in string.gmatch(cur, '[^,]+') do
  if p == idx then return 0 end
end
redis.call('HSET', KEYS[1], 'uploaded', cur .. ',' .. idx)
return 1
"""


def _is_valid_md5(value: str) -> bool:
    return bool(value) and _MD5_RE.fullmatch(value) is not None


def _chunk_task_key(user: str, md5_val: str) -> str:
    """分片任务 Redis 键（按用户隔离，避免不同用户上传相同内容时互相干扰/抢占任务）"""
    return f"chunk:{user}:{md5_val}"


def _chunk_dir(user: str, md5_val: str) -> str:
    """分片临时目录（按用户隔离；用户名哈希后拼路径，杜绝特殊字符进入路径）"""
    user_dir = hashlib.md5(user.encode()).hexdigest()
    return os.path.join(CHUNK_TEMP_DIR, user_dir, md5_val)


def _cleanup_stale_chunks(user: str):
    """删除该用户超过 TTL 仍残留的分片目录/临时合并文件。

    Redis 任务 24h 过期只清元数据，分片文件此前无任何清理路径——
    放弃的上传会永久占用 chunk_data 卷。挂在该用户 chunk_init 时惰性执行，
    只动自己的目录，多 worker 并发触发时重复删除被 ignore_errors 吸收。
    目录 mtime 在写入分片时更新，活跃上传不会被误删。
    """
    root = os.path.join(CHUNK_TEMP_DIR, hashlib.md5(user.encode()).hexdigest())
    cutoff = time.time() - CHUNK_STALE_SECONDS
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)  # 残留的 *_merged 临时文件
            except OSError:
                continue
    except OSError:
        pass  # 用户目录不存在 = 无垃圾可清


def _merge_chunk_files(chunk_dir: str, chunk_count: int, merged_path: str):
    """顺序合并分片（1MB 块流式读写，避免整段读入内存）"""
    with open(merged_path, "wb") as out:
        for i in range(chunk_count):
            with open(os.path.join(chunk_dir, str(i)), "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    out.write(block)


def _md5_of_file(path: str) -> str:
    """流式计算文件 MD5（1MB 块），大文件不会撑爆内存"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


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

    if not _is_valid_md5(file_md5):
        return {"code": 1, "msg": "invalid md5"}
    if not isinstance(chunk_count, int) or chunk_count < 1 or chunk_count > _MAX_CHUNK_COUNT:
        return {"code": 1, "msg": "invalid chunkCount"}

    redis_key = _chunk_task_key(user, file_md5)

    # 惰性清理该用户超过 TTL 的废弃分片（磁盘 IO 挪线程池）
    await asyncio.to_thread(_cleanup_stale_chunks, user)

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
    os.makedirs(_chunk_dir(user, file_md5), exist_ok=True)

    return {"code": 0, "chunkCount": chunk_count, "uploadedChunks": ""}


@router.post("/chunk_upload")
async def chunk_upload(request: Request):
    """
    分片上传 —— 接收单个分片的二进制数据，保存到本地临时目录。
    必须携带 user + token（且与任务发起人一致），限制单片大小，
    防止未授权写入与他人恶意污染/灌盘。
    user/token 优先读请求头（避免 token 进 URL 查询串被访问日志留存），
    兼容旧客户端的查询串传法。
    """
    qs = parse_qs(request.url.query)
    md5_val = (qs.get("md5") or [""])[0]
    user = request.headers.get("x-upload-user") or (qs.get("user") or [""])[0]
    token = request.headers.get("x-upload-token") or (qs.get("token") or [""])[0]
    idx_raw = (qs.get("index") or [""])[0]

    # 只接受 32 位十六进制 md5，防止路径穿越写任意路径
    if not _is_valid_md5(md5_val):
        return {"code": 1, "msg": "invalid md5"}
    try:
        idx = int(idx_raw)
    except ValueError:
        return {"code": 1, "msg": "invalid index"}
    if idx < 0 or idx >= _MAX_CHUNK_COUNT:
        return {"code": 1, "msg": "invalid index"}

    # 鉴权：token 有效，且请求者必须是该分片任务的发起人
    if not await check_token(user, token):
        return {"code": 4}
    redis_key = _chunk_task_key(user, md5_val)
    chunk_count_raw = await redis.hget(redis_key, "chunk_count")
    if not chunk_count_raw:
        return {"code": 1, "msg": "chunk task not found"}
    # 分片序号必须落在任务声明的 chunk_count 之内：否则伪分片能把 uploaded
    # 列表长度凑够 chunk_count，骗过完整性检查后 merge 打开不存在的文件
    if idx >= int(chunk_count_raw):
        return {"code": 1, "msg": "invalid index"}

    # 单片大小限制：先看声明长度（提前拒绝），再校验实际 body
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_CHUNK_BODY:
                return {"code": 1, "msg": "chunk too large"}
        except ValueError:
            return {"code": 1, "msg": "invalid content-length"}

    body = await request.body()
    if len(body) > _MAX_CHUNK_BODY:
        return {"code": 1, "msg": "chunk too large"}

    # 保存分片到临时文件
    chunk_dir = _chunk_dir(user, md5_val)
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, str(idx))
    with open(chunk_path, "wb") as f:
        f.write(body)

    # 原子更新 Redis 中已上传分片列表（Lua，防并发读改写覆盖）
    await redis.eval(_ADD_UPLOADED_LUA, 1, redis_key, str(idx))

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
    if not _is_valid_md5(md5_val):
        return {"code": 1, "msg": "invalid md5"}
    redis_key = _chunk_task_key(user, md5_val)

    chunk_count_str = await redis.hget(redis_key, "chunk_count")
    if not chunk_count_str:
        return {"code": 1}

    chunk_count = int(chunk_count_str)
    uploaded_str = await redis.hget(redis_key, "uploaded") or ""
    uploaded = [int(x) for x in uploaded_str.split(",") if x]

    # 检查所有分片是否都已上传
    if len(uploaded) != chunk_count:
        return {"code": 1, "msg": f"分片不完整: {len(uploaded)}/{chunk_count}"}

    # 合并分片到完整文件（目录按用户隔离；磁盘 IO 挪线程池，不阻塞事件循环）
    chunk_dir = _chunk_dir(user, md5_val)
    merged_path = chunk_dir + "_merged"
    await asyncio.to_thread(_merge_chunk_files, chunk_dir, chunk_count, merged_path)

    # 实际 MD5 校验（流式，1MB 块）
    actual_md5 = await asyncio.to_thread(_md5_of_file, merged_path)
    if actual_md5 != md5_val:
        await asyncio.to_thread(os.remove, merged_path)
        return {"code": 1, "msg": "MD5 校验失败"}

    file_size = os.path.getsize(merged_path)
    suffix = _get_suffix(filename)

    async with Session() as db:
        # MD5 去重：检查是否已有相同文件
        from sqlalchemy import select as _select
        result = await db.execute(
            _select(FileInfo).where(FileInfo.md5 == md5_val).with_for_update()
        )
        existing = result.scalar()

        if existing:
            existing.count += 1
            url = existing.url
        else:
            await asyncio.to_thread(ensure_bucket)
            # 对象名使用完整 md5（合并前已做内容 MD5 校验），避免前缀碰撞覆盖
            object_name = f"{md5_val}/{filename}"
            with open(merged_path, "rb") as f:
                await asyncio.to_thread(client.put_object, BUCKET, object_name, f, file_size)
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

        try:
            await db.commit()
        except IntegrityError:
            # 并发合并相同内容：另一请求已抢先创建 file_info —— 补建本用户关联而非丢弃
            await db.rollback()
            import shutil
            os.remove(merged_path)
            shutil.rmtree(chunk_dir, ignore_errors=True)
            await redis.delete(redis_key)
            return {"code": 0 if await attach_file_to_user(user, md5_val, filename) else 5}

    # 清理 Redis 和临时文件
    await redis.delete(redis_key)
    await asyncio.to_thread(os.remove, merged_path)
    import shutil
    await asyncio.to_thread(shutil.rmtree, chunk_dir, ignore_errors=True)

    return {"code": 0}
