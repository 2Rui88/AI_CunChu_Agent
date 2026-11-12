"""
Agent 工具集 —— 8 个文件操作工具的纯实现，供 ReAct Agent Loop 调用

本模块只放实现；工具的登记（LLM 描述 / 参数 Schema / 危险标记 / 能力
文案）统一在 tool_registry.py 的一张工具表里，路由层经注册表间接调用。

每个工具函数签名统一为: async def tool_xxx(username: str, **kwargs) -> dict
- username: 当前用户（由 Agent 注入，非 LLM 参数）
- kwargs: 来自 LLM function_call 的参数

注：对话式助手是全站唯一的文件搜索入口——search_files 委托
    ai.semantic_search 全链路（一次 LLM 查询规划 + 双路召回 + 统一 RRF +
    候选>5 时重排），独立的搜索框与 /ai/search 端点已移除。
    read_file 是 describe_file 的内容侧补位：描述是上传时算好的快照
    （文本类含前 3 万字符并带截断标记），read_file 按字节偏移分页读
    MinIO 原文，两者配合覆盖"文件里到底写了什么"类提问。
"""
import re
import asyncio
import logging
from sqlalchemy import select, delete as sql_delete
from app.database import Session
from app.config import settings
from app.models import (
    UserFileList, FileInfo, UserFileCount,
    ShareFileList, UserFileAiDesc, FileAiDesc,
)
from app.redis_client import redis
from app.faiss_service import mark_dirty
from app.share_stats import bump_public_count, plaza_add, plaza_remove
from app.minio_client import client as minio_client, BUCKET, presign_from_db_url


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


# ── read_file 类型门 ──
# 扩展名白名单：与 ai._TEXT_TYPES 对齐并扩充常见代码/配置类型。
# PDF/Excel/docx 的提取文本已在 AI 描述中（describe_file 可看），
# 但原始字节无文本语义，不入白名单；图片/视频/压缩包同理。
_READABLE_TEXT_TYPES = {
    "txt", "md", "log", "csv", "tsv", "json", "xml", "yaml", "yml", "toml",
    "ini", "conf", "cfg", "env", "html", "htm", "css", "js", "ts", "vue",
    "py", "java", "c", "cpp", "h", "hpp", "go", "rs", "sql", "sh", "bat",
    "ps1", "rb", "php", "gitignore",
}
# 扩展名为 unknown 但按文件名放行的特例（无扩展名文本文件）
_READABLE_TEXT_NAMES = {"dockerfile", "makefile", "readme", "license"}


def _is_readable_text(filename: str) -> bool:
    """read_file 类型门：扩展名白名单 + 无扩展名文本文件的文件名特例"""
    if _get_suffix(filename) in _READABLE_TEXT_TYPES:
        return True
    return (filename or "").lower() in _READABLE_TEXT_NAMES


def _clamp_read_params(offset_raw, length_raw) -> tuple[int, int]:
    """钳制 LLM 给出的 offset/length（null/负数/超界值就地修正，不抛错）"""
    try:
        offset = max(0, int(offset_raw or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        length = max(
            settings.read_file_min_chars,
            min(int(length_raw or settings.read_file_default_chars),
                settings.read_file_max_chars),
        )
    except (TypeError, ValueError):
        length = settings.read_file_default_chars
    return offset, length


def _align_and_decode(data: bytes, max_chars: int, at_file_end: bool = False) -> tuple[str, str, int]:
    """
    行对齐 + 解码一段原始字节，返回 (文本, 编码, 实际消费字节数)。

    先在 max_chars*4 字节窗口内找最后一个换行截断（UTF-8 最坏一字 4
    字节），返回内容始终是完整行；整段无换行（超长单行，如压缩过的
    JS）按窗口上限硬截。at_file_end 表示缓冲已覆盖文件末尾——此时尾
    部残行就是文件最后一行，直接消费不再对齐截断，避免 eof 被无谓地
    推迟到下一次调用。硬截可能切在多字节序列中间：退 1~3 字节重试解
    码；仍失败说明本来就不是 UTF-8，回落 latin-1（与描述管线的
    _decode_text 策略一致，保证两边读到的内容逐字对齐）。
    """
    window = data[:max_chars * 4]
    nl = window.rfind(b"\n")
    if at_file_end:
        consumed = len(window)
    elif nl >= 0:
        consumed = nl + 1
    else:
        consumed = len(window)
    chunk = data[:consumed]
    for drop in range(0, 4):
        end = consumed - drop
        if end <= 0:
            break
        try:
            return chunk[:end].decode("utf-8"), "utf-8", end
        except UnicodeDecodeError:
            continue
    return chunk.decode("latin-1"), "latin-1", consumed


def _get_object_range(file_id: str, offset: int, length: int) -> bytes:
    """MinIO 字节范围读（同步阻塞，调用方需 asyncio.to_thread 包裹）"""
    resp = minio_client.get_object(BUCKET, file_id, offset=offset, length=length)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


async def tool_read_file(username: str, **kwargs) -> dict:
    """
    分页读取文本文件的原始内容（MinIO 字节范围读 + 行对齐）。
    LLM 参数: identifier (str) — md5 或文件名
              offset (int, 可选, 默认0) — 起始字节位置
              length (int, 可选, 默认4000) — 期望读取的字符数
    响应信封带 next_offset / eof：LLM 抄回 next_offset 续读，eof=true 停。
    """
    identifier = kwargs.get("identifier", "")
    if not identifier:
        return {"error": "missing identifier"}
    offset, length = _clamp_read_params(kwargs.get("offset"), kwargs.get("length"))

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

            # 类型门：白名单外零下载成本拒绝，指路 describe_file
            if not _is_readable_text(ufl.file_name):
                return {
                    "success": False,
                    "reason": "unsupported_type",
                    "filename": ufl.file_name,
                    "type": fi.type or _get_suffix(ufl.file_name),
                    "message": "该文件类型无文本内容，可调用 describe_file 查看其 AI 描述",
                }

            size = int(fi.size or 0)
            if size == 0:
                return {"success": True, "md5": ufl.md5, "filename": ufl.file_name,
                        "size": 0, "offset": 0, "content": "", "chars": 0,
                        "next_offset": None, "eof": True, "encoding": "utf-8"}
            if offset >= size:
                return {"success": False, "filename": ufl.file_name,
                        "size": size,
                        "message": f"offset={offset} 已超出文件末尾（size={size}）"}

            # 字节范围读：×4 覆盖 UTF-8 最坏情况，+slack 供行对齐扫描
            fetch_len = min(length * 4 + settings.read_file_slack_bytes, size - offset)
            data = await asyncio.to_thread(_get_object_range, fi.file_id, offset, fetch_len)

        content, encoding, consumed = _align_and_decode(
            data, length, at_file_end=offset + len(data) >= size,
        )
        next_offset = offset + consumed
        eof = next_offset >= size
        return {
            "success": True,
            "md5": ufl.md5,
            "filename": ufl.file_name,
            "size": size,
            "offset": offset,
            "content": content,
            "chars": len(content),
            "next_offset": None if eof else next_offset,
            "eof": eof,
            "encoding": encoding,
        }
    except Exception as exc:
        return {"error": f"read file failed: {exc}"}


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
            fileid = f"{ufl.md5}{ufl.file_name}"
            was_shared = ufl.shared_status == 1

            # 清理分享记录（Redis 部分推迟到 commit 之后）
            if was_shared:
                await db.execute(
                    sql_delete(ShareFileList).where(
                        ShareFileList.user == username,
                        ShareFileList.md5 == ufl.md5,
                        ShareFileList.file_name == ufl.file_name,
                    )
                )
                # 部门公共分享计数 -1（与 share.py _del_file 同一部门化助手）
                await bump_public_count(db, ufl.department_id, -1)

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

    # 提交成功后的收尾：Redis 清理（部门化 key）+ MinIO 对象删除
    if fileid and was_shared:
        try:
            await plaza_remove(ufl.department_id, fileid)
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
            dept_id = ufl.department_id

            # 检查是否被本部门他人分享（共享语义按部门隔离）
            dup = await db.execute(
                select(ShareFileList).where(
                    ShareFileList.department_id == dept_id,
                    ShareFileList.md5 == ufl.md5,
                    ShareFileList.file_name == ufl.file_name,
                ).limit(1)
            )
            if dup.scalar():
                return {"success": False, "message": f"文件 '{ufl.file_name}' 已被分享"}

            ufl.shared_status = 1
            db.add(ShareFileList(
                user=username, md5=ufl.md5, file_name=ufl.file_name, pv=0,
                department_id=dept_id,
            ))

            # 部门公共计数 +1
            await bump_public_count(db, dept_id, 1)

            await db.commit()
    except Exception as exc:
        return {"error": f"share failed: {exc}"}

    # Redis 更新（部门化 key，非关键，失败不影响）
    try:
        await plaza_add(dept_id, fileid, ufl.file_name)
    except Exception:
        pass

    return {"success": True, "md5": ufl.md5, "filename": ufl.file_name,
            "message": f"文件 '{ufl.file_name}' 已分享到共享广场"}
