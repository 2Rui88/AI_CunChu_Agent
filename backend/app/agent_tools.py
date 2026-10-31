"""
Agent 工具集 —— 7 个文件操作工具，供 ReAct Agent Loop 调用

每个工具函数签名统一为: async def tool_xxx(username: str, **kwargs) -> dict
- username: 当前用户（由 Agent 注入，非 LLM 参数）
- kwargs: 来自 LLM function_call 的参数

工具类型:
  READ:        search_files, get_file_info, list_recent_files,
               get_storage_stats, describe_file
  DESTRUCTIVE: delete_file, share_file（需用户二次确认）

注：对话式助手是全站唯一的文件搜索入口——search_files 委托
    ai.semantic_search 全链路（一次 LLM 查询规划 + 双路召回 + 统一 RRF +
    候选>5 时重排），独立的搜索框与 /ai/search 端点已移除。
"""
import re
import asyncio
import logging
from sqlalchemy import select, delete as sql_delete
from app.database import Session
from app.models import (
    UserFileList, FileInfo, UserFileCount,
    ShareFileList, UserFileAiDesc, FileAiDesc,
)
from app.redis_client import redis
from app.faiss_service import mark_dirty
from app.minio_client import client as minio_client, BUCKET, presign_from_db_url

# ── 危险操作集合（Agent 确认门控用）──
DESTRUCTIVE_TOOLS = {"delete_file", "share_file"}


def _compress_description(text: str) -> str:
    """
    压缩描述文本为前两句，减少 Agent LLM token 消耗。
    句子分隔符：。！？!? \n
    超过两句时以 "..." 结尾。
    """
    if not text:
        return ""
    sentences = re.split(r"[。！？!?\n]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 2:
        return text
    return "。".join(sentences[:2]) + "。"


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
    语义搜索用户文件（全站唯一搜索入口）。
    LLM 参数: query (str), top_k (int, 可选, 默认5)
    检索委托 ai.semantic_search 全链路：一次 LLM 查询规划 + 双路召回 + 统一 RRF 融合 + 候选>5 时重排序。
    """
    query = kwargs.get("query", "")
    # top_k 钳制（LLM 给出的参数不可信）
    try:
        top_k = max(1, min(int(kwargs.get("top_k", 5) or 5), 20))
    except (TypeError, ValueError):
        top_k = 5
    if not query:
        return {"error": "missing query"}

    # 懒加载避免 agent 工具模块与路由模块的循环导入
    from app.routers.ai import semantic_search

    result = await semantic_search(username, query, top_k=top_k)
    if result.get("code") != 0:
        return {"error": result.get("msg", "search failed")}

    files = result.get("files", [])[:top_k]
    # 压缩描述：仅保留描述的前两句，减少 LLM token 消耗
    for f in files:
        f["description"] = _compress_description(f.get("description") or "")

    if not files:
        return {"count": 0, "files": [], "message": "没有找到匹配的文件"}
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
            # 下载量只统计共享广场的下载，取 share_file_list.pv
            pv = 0
            if ufl.shared_status == 1:
                share_result = await db.execute(
                    select(ShareFileList.pv).where(
                        ShareFileList.user == username,
                        ShareFileList.md5 == ufl.md5,
                        ShareFileList.file_name == ufl.file_name,
                    ).limit(1)
                )
                share_row = share_result.first()
                if share_row:
                    pv = share_row[0] or 0
            return {
                "found": True,
                "md5": ufl.md5,
                "filename": ufl.file_name,
                "size": fi.size,
                "type": fi.type,
                "url": presign_from_db_url(fi.url) if fi.url else "",
                "pv": pv,
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
    # 解析在 try 内并双向钳制：LLM 传 count=null 会让 int(None) 抛 TypeError
    # 白耗一轮重试，传 0/负数则返回空列表
    try:
        count = max(1, min(int(kwargs.get("count") or 10), 50))
    except (TypeError, ValueError):
        count = 10

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
                    "url": presign_from_db_url(fi.url) if fi.url else "",
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

            # 下载量只统计共享广场的下载，取该用户已分享文件的累计下载量
            return {
                "file_count": total_files,
                "shared_count": len(shared),
                "total_downloads": sum(s.pv or 0 for s in shared),
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
    MinIO 对象删除放在事务提交之后（与 share.py _del_file 对齐）：
    提交前删对象而 commit 失败会弄丢仍被引用的文件数据，提交后删失败
    只是留下记日志的孤儿对象。
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}

    fileid = None
    object_to_remove = None

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

            # 清理分享记录（Redis 部分推迟到 commit 之后）
            if ufl.shared_status == 1:
                await db.execute(
                    sql_delete(ShareFileList).where(
                        ShareFileList.user == username,
                        ShareFileList.md5 == ufl.md5,
                        ShareFileList.file_name == ufl.file_name,
                    )
                )
                # 公共分享计数 -1（与 share.py _del_file 对齐，修正历史漂移）
                result = await db.execute(
                    select(UserFileCount).where(UserFileCount.user == "FILE_PUBLIC_COUNT")
                )
                ufc_pub = result.scalar()
                if ufc_pub and ufc_pub.count > 0:
                    ufc_pub.count -= 1
                fileid = f"{ufl.md5}{ufl.file_name}"

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

            # 引用计数处理：锁定 file_info，避免并发删除丢失减量
            locked = await db.execute(
                select(FileInfo).where(FileInfo.md5 == ufl.md5).with_for_update()
            )
            fi = locked.scalar_one_or_none()
            if fi is not None:
                fi.count -= 1
                if fi.count <= 0:
                    object_to_remove = fi.file_id
                    await db.delete(fi)
            # file_info 已被并发删除（引用恰好归零）时无需引用处理，继续删除用户关联

            await db.commit()
    except Exception as exc:
        return {"error": f"delete failed: {exc}"}

    # 提交成功后的收尾：Redis 清理 + MinIO 对象删除
    if fileid:
        try:
            await redis.zrem("FILE_PUBLIC_ZSET", fileid)
            await redis.hdel("FILE_NAME_HASH", fileid)
        except Exception:
            pass
    if object_to_remove:
        try:
            await asyncio.to_thread(minio_client.remove_object, BUCKET, object_to_remove)
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "MinIO 对象删除失败（孤儿对象残留）bucket=%s object=%s",
                BUCKET, object_to_remove,
            )

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
            db.add(ShareFileList(user=username, md5=ufl.md5, file_name=ufl.file_name, pv=0))

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
