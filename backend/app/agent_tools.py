"""
Agent 工具集 —— 7 个文件操作工具，供 ReAct Agent Loop 调用

每个工具函数签名统一为: async def tool_xxx(username: str, **kwargs) -> dict
- username: 当前用户（由 Agent 注入，非 LLM 参数）
- kwargs: 来自 LLM function_call 的参数

工具类型:
  READ:        search_files, get_file_info, list_recent_files,
               get_storage_stats, describe_file
  DESTRUCTIVE: delete_file, share_file（需用户二次确认）
"""
import numpy as np
from sqlalchemy import select, delete as sql_delete
from app.database import Session
from app.models import (
    UserFileList, FileInfo, UserFileCount,
    ShareFileList, UserFileAiDesc, FileAiDesc,
)
from app.redis_client import redis
from app.config import settings
from app.dashscope_client import get_embedding
from app.faiss_service import (
    search as faiss_search,
    add_vector,
    l2_normalize, get_ntotal,
    mark_dirty,
)
from app.minio_client import client as minio_client, BUCKET

# ── 危险操作集合（Agent 确认门控用）──
DESTRUCTIVE_TOOLS = {"delete_file", "share_file"}


def _get_suffix(filename: str) -> str:
    """从文件名提取扩展名"""
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "unknown"


# ============================================================
#  READ 工具
# ============================================================

async def tool_search_files(username: str, **kwargs) -> dict:
    """
    语义搜索用户文件。
    LLM 参数: query (str), top_k (int, 可选, 默认5)
    """
    query = kwargs.get("query", "")
    top_k = int(kwargs.get("top_k", 5))
    api_key = kwargs.get("api_key", "")

    if not query or not api_key:
        return {"error": "missing query or api_key"}

    try:
        embedding = await get_embedding(api_key, query)
        query_vec = np.array(embedding, dtype=np.float32)
    except Exception as exc:
        return {"error": f"embedding failed: {exc}"}

    try:
        results = faiss_search(username, query_vec, max(top_k, 20))
    except Exception as exc:
        return {"error": f"faiss search failed: {exc}"}

    if not results:
        return {"count": 0, "files": [], "message": "没有找到匹配的文件"}

    files = []
    try:
        async with Session() as db:
            for faiss_id, score in results:
                if score < 0.45:
                    continue
                result = await db.execute(
                    select(UserFileAiDesc, UserFileList, FileInfo)
                    .join(UserFileList,
                          (UserFileList.user == UserFileAiDesc.user) &
                          (UserFileList.md5 == UserFileAiDesc.md5))
                    .join(FileInfo, FileInfo.md5 == UserFileAiDesc.md5)
                    .where(
                        UserFileAiDesc.user == username,
                        UserFileAiDesc.faiss_id == faiss_id,
                        UserFileAiDesc.status == 1,
                    ).limit(1)
                )
                row = result.first()
                if row:
                    uad, ufl, fi = row
                    files.append({
                        "md5": uad.md5,
                        "filename": ufl.file_name,
                        "description": uad.description,
                        "url": fi.url,
                        "size": str(fi.size),
                        "type": fi.type,
                        "score": round(score, 4),
                    })
    except Exception as exc:
        return {"error": f"database query failed: {exc}"}

    return {"count": len(files), "files": files}


async def tool_get_file_info(username: str, **kwargs) -> dict:
    """
    获取指定文件的详细信息。
    LLM 参数: identifier (str) — md5 或文件名
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}

    try:
        async with Session() as db:
            result = await db.execute(
                select(UserFileList, FileInfo)
                .join(FileInfo, UserFileList.md5 == FileInfo.md5)
                .where(
                    UserFileList.user == username,
                    (UserFileList.md5 == identifier) |
                    (UserFileList.file_name == identifier),
                ).limit(1)
            )
            row = result.first()
            if not row:
                return {"found": False, "message": f"未找到文件 '{identifier}'"}

            ufl, fi = row
            return {
                "found": True,
                "md5": ufl.md5,
                "filename": ufl.file_name,
                "size": fi.size,
                "type": fi.type,
                "url": fi.url,
                "pv": ufl.pv,
                "shared": ufl.shared_status == 1,
                "create_time": str(ufl.create_time),
            }
    except Exception as exc:
        return {"error": f"database query failed: {exc}"}


async def tool_list_recent_files(username: str, **kwargs) -> dict:
    """
    列出用户最近上传的文件。
    LLM 参数: count (int, 可选, 默认10)
    """
    count = min(int(kwargs.get("count", 10)), 50)

    try:
        async with Session() as db:
            result = await db.execute(
                select(UserFileList, FileInfo)
                .join(FileInfo, UserFileList.md5 == FileInfo.md5)
                .where(UserFileList.user == username)
                .order_by(UserFileList.create_time.desc())
                .limit(count)
            )
            rows = result.all()
            files = []
            for ufl, fi in rows:
                files.append({
                    "md5": ufl.md5,
                    "filename": ufl.file_name,
                    "size": fi.size,
                    "type": fi.type,
                    "url": fi.url,
                    "create_time": str(ufl.create_time),
                })
            return {"count": len(files), "files": files}
    except Exception as exc:
        return {"error": f"database query failed: {exc}"}


async def tool_get_storage_stats(username: str, **kwargs) -> dict:
    """获取用户存储统计信息。"""
    try:
        async with Session() as db:
            file_count = await db.execute(
                select(UserFileCount).where(UserFileCount.user == username)
            )
            ufc = file_count.scalar()
            total_files = ufc.count if ufc else 0

            shared_count = await db.execute(
                select(ShareFileList).where(ShareFileList.user == username)
            )
            shared = shared_count.scalars().all()

            total_downloads = await db.execute(
                select(UserFileList.pv).where(UserFileList.user == username)
            )
            pv_sum = sum(r[0] or 0 for r in total_downloads.all())

            return {
                "file_count": total_files,
                "shared_count": len(shared),
                "total_downloads": pv_sum,
            }
    except Exception as exc:
        return {"error": f"database query failed: {exc}"}


async def tool_describe_file(username: str, **kwargs) -> dict:
    """
    获取或生成文件的 AI 描述。
    LLM 参数: identifier (str) — md5 或文件名
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}

    try:
        async with Session() as db:
            # 先查用户文件
            result = await db.execute(
                select(UserFileList).where(
                    UserFileList.user == username,
                    (UserFileList.md5 == identifier) |
                    (UserFileList.file_name == identifier),
                ).limit(1)
            )
            ufl = result.scalar()
            if not ufl:
                return {"success": False, "message": f"文件 '{identifier}' 不存在"}

            # 查用户 AI 描述
            result = await db.execute(
                select(UserFileAiDesc).where(
                    UserFileAiDesc.user == username,
                    UserFileAiDesc.md5 == ufl.md5,
                    UserFileAiDesc.status == 1,
                ).limit(1)
            )
            user_desc = result.scalar()
            if user_desc:
                return {
                    "success": True,
                    "md5": ufl.md5,
                    "filename": ufl.file_name,
                    "description": user_desc.description,
                    "source": "existing",
                }

            # 查全局缓存
            result = await db.execute(
                select(FileAiDesc).where(
                    FileAiDesc.md5 == ufl.md5, FileAiDesc.status == 1
                ).limit(1)
            )
            cache = result.scalar()
            if cache:
                return {
                    "success": True,
                    "md5": ufl.md5,
                    "filename": ufl.file_name,
                    "description": cache.description,
                    "source": "cached",
                }

            return {
                "success": False,
                "md5": ufl.md5,
                "filename": ufl.file_name,
                "message": "该文件尚未生成 AI 描述",
            }
    except Exception as exc:
        return {"error": f"database query failed: {exc}"}


# ============================================================
#  DESTRUCTIVE 工具（需用户确认）
# ============================================================

async def tool_delete_file(username: str, **kwargs) -> dict:
    """
    删除用户文件（危险操作）。
    LLM 参数: identifier (str) — md5 或文件名
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}

    try:
        async with Session() as db:
            result = await db.execute(
                select(UserFileList, FileInfo)
                .join(FileInfo, UserFileList.md5 == FileInfo.md5)
                .where(
                    UserFileList.user == username,
                    (UserFileList.md5 == identifier) |
                    (UserFileList.file_name == identifier),
                ).limit(1)
            )
            row = result.first()
            if not row:
                return {"success": False, "message": f"文件 '{identifier}' 不存在"}

            ufl, fi = row

            # 清理分享记录
            if ufl.shared_status == 1:
                await db.execute(
                    sql_delete(ShareFileList).where(
                        ShareFileList.user == username,
                        ShareFileList.md5 == ufl.md5,
                        ShareFileList.file_name == ufl.file_name,
                    )
                )
                fileid = f"{ufl.md5}{ufl.file_name}"
                try:
                    await redis.zrem("FILE_PUBLIC_ZSET", fileid)
                    await redis.hdel("FILE_NAME_HASH", fileid)
                except Exception:
                    pass

            # 删除 AI 记录
            await db.execute(
                sql_delete(UserFileAiDesc).where(
                    UserFileAiDesc.user == username,
                    UserFileAiDesc.md5 == ufl.md5,
                )
            )

            # 删除用户文件关联
            await db.delete(ufl)

            # 更新计数
            result = await db.execute(
                select(UserFileCount).where(UserFileCount.user == username)
            )
            ufc = result.scalar()
            if ufc and ufc.count > 0:
                ufc.count -= 1

            # 引用计数处理
            fi.count -= 1
            if fi.count <= 0:
                try:
                    minio_client.remove_object(BUCKET, fi.file_id)
                except Exception:
                    pass
                await db.delete(fi)

            await db.commit()
    except Exception as exc:
        return {"error": f"delete failed: {exc}"}

    # 标记 FAISS 脏
    try:
        mark_dirty(username)
    except Exception:
        pass

    return {"success": True, "md5": ufl.md5, "filename": ufl.file_name,
            "message": f"文件 '{ufl.file_name}' 已删除"}


async def tool_share_file(username: str, **kwargs) -> dict:
    """
    分享文件到共享广场（危险操作）。
    LLM 参数: identifier (str) — md5 或文件名
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}

    try:
        async with Session() as db:
            result = await db.execute(
                select(UserFileList).where(
                    UserFileList.user == username,
                    (UserFileList.md5 == identifier) |
                    (UserFileList.file_name == identifier),
                ).limit(1)
            )
            ufl = result.scalar()
            if not ufl:
                return {"success": False, "message": f"文件 '{identifier}' 不存在"}

            if ufl.shared_status == 1:
                return {"success": False, "message": f"文件 '{ufl.file_name}' 已分享过"}

            fileid = f"{ufl.md5}{ufl.file_name}"

            # 检查是否被他人分享
            dup = await db.execute(
                select(ShareFileList).where(
                    ShareFileList.md5 == ufl.md5,
                    ShareFileList.file_name == ufl.file_name,
                ).limit(1)
            )
            if dup.scalar():
                return {"success": False, "message": f"文件 '{ufl.file_name}' 已被分享"}

            ufl.shared_status = 1
            db.add(ShareFileList(user=username, md5=ufl.md5, file_name=ufl.file_name))

            # 公共计数 +1
            result = await db.execute(
                select(UserFileCount).where(UserFileCount.user == "FILE_PUBLIC_COUNT")
            )
            ufc = result.scalar()
            if ufc:
                ufc.count += 1
            else:
                db.add(UserFileCount(user="FILE_PUBLIC_COUNT", count=1))

            await db.commit()
    except Exception as exc:
        return {"error": f"share failed: {exc}"}

    # Redis 更新（非关键，失败不影响）
    try:
        await redis.zadd("FILE_PUBLIC_ZSET", {fileid: 0})
        await redis.hset("FILE_NAME_HASH", fileid, ufl.file_name)
    except Exception:
        pass

    return {"success": True, "md5": ufl.md5, "filename": ufl.file_name,
            "message": f"文件 '{ufl.file_name}' 已分享到共享广场"}


# ── 工具注册表 ──

TOOLS = {
    "search_files": tool_search_files,
    "get_file_info": tool_get_file_info,
    "list_recent_files": tool_list_recent_files,
    "get_storage_stats": tool_get_storage_stats,
    "describe_file": tool_describe_file,
    "delete_file": tool_delete_file,
    "share_file": tool_share_file,
}
