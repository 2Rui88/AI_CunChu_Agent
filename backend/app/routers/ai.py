"""
AI 智能检索模块 —— describe / search / rebuild

describe: 生成文件 AI 描述 + 向量 → 存入 MySQL + FAISS
  - 图片(png/jpg/...) → Qwen-VL 多模态描述
  - 文本(txt/md/py/...) → 下载读取内容作为描述
  - docx → 解压提取 XML 文本作为描述
  - 其他 → 类型 + 文件名拼接描述
search:   语义搜索引擎 semantic_search（一次 LLM 查询规划 + 双路召回 +
          统一 RRF + 候选>5 时重排），不再暴露独立 HTTP 端点——
          对话助手 search_files 工具是唯一调用方
rebuild:  从 MySQL 全量重建用户 FAISS 索引
"""
import io
import re
import json
import time
import html
import asyncio
import logging
import zipfile
import fitz  # PyMuPDF
import openpyxl
from app.chunker import chunk_text, Chunk
import numpy as np
import httpx
from fastapi import APIRouter
from sqlalchemy import select, text, delete
from app.database import Session
from app.models import FileInfo, UserFileList, FileAiDesc, UserFileAiDesc
from app.dependencies import check_token
from app.config import settings
from app.minio_client import presign_url, presign_internal_url, presign_from_db_url, db_url_to_object
from app.dashscope_client import describe_image, get_embedding, create_client
from app.faiss_service import (
    search as faiss_search,
    add_vector,
    rebuild_locked,
    l2_normalize,
    get_ntotal,
    load_index,
    is_dirty, clear_dirty_if_older, mark_dirty,
    acquire_lock, release_lock,
)

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["ai"])

# 兼容旧 C 项目的分数阈值
SCORE_THRESHOLD = 0.45

# ── 内容提取三层策略 ──
# 体积门槛（parse_max_mb）只做解析安全线；索引成本由字符预算（extract_char_budget）控制；
# 超过安全线/提取失败时退化为结构化存根，并由用户手动决定是否花成本建立内容索引。

_TOO_LARGE_MSG = "文件超过解析上限"
_EXTRACT_FAIL_MSG = "内容提取失败"


class _DownloadTooLarge(Exception):
    """文件体积超过解析安全线（在下载中途即断开，内存有界）"""


def _normal_cap() -> int:
    """常规提取的解析安全线（字节）"""
    return settings.parse_max_mb * 1024 * 1024


def _forced_cap() -> int:
    """手动"建立内容索引"时的硬上限（字节）"""
    return max(settings.parse_max_mb_forced, settings.parse_max_mb) * 1024 * 1024


def _human_size(n) -> str:
    if n is None:
        return "未知"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def _make_stub_description(file_type: str, filename: str, size, reason: str) -> str:
    """降级存根：至少让文件名/类型/大小可被语义检索，并明确标记未做内容索引"""
    return (
        f"文件名：{filename}\n"
        f"类型：{file_type or '未知'}类型文件\n"
        f"大小：{_human_size(size)}\n"
        f"状态：{reason}，仅建立文件名索引；可在文件列表中手动建立内容索引"
    )


async def _download_capped(file_url: str, cap_bytes: int) -> bytes:
    """
    流式下载并在超限时立即中止：
    - 先按 Content-Length 预检（有则一个字节都不下载）
    - 下载中逐块累计，超限即断开连接
    内存占用最多约 cap_bytes，避免大文件先全量读入再检查。
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", file_url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"unexpected status {resp.status_code}")
                declared = resp.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > cap_bytes:
                            raise _DownloadTooLarge()
                    except ValueError:
                        pass
                buf = bytearray()
                async for block in resp.aiter_bytes(1024 * 1024):
                    buf.extend(block)
                    if len(buf) > cap_bytes:
                        raise _DownloadTooLarge()
                return bytes(buf)
    except _DownloadTooLarge:
        raise
    except Exception as exc:
        raise RuntimeError(f"download failed: {exc}") from exc


def _build_public_url(db_url: str) -> str:
    """
    供 DashScope（Qwen-VL）下载图片的公网 URL。
    桶已私有化，必须带预签名；scheme/host 由 public_server_* 配置决定。
    """
    return presign_url(db_url_to_object(db_url), expires=3600)


# ============================================================
#  describe —— AI 文件描述 + 向量化
# ============================================================

@router.post("/ai/describe")
async def ai_describe(body: dict):
    """
    为指定文件生成 AI 描述和向量，存入 file_ai_desc（全局缓存）和
    user_file_ai_desc（用户记录），并追加到用户 FAISS 索引。
    需要: user, token, md5, filename, type；API Key 由服务端统一配置
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    # 密钥统一取服务端配置（客户端传入的 api_key 一律忽略，防泄露）
    api_key = settings.dashscope_api_key
    if not api_key:
        return {"code": 1, "msg": "AI 服务未配置，请联系管理员"}

    md5_val = body.get("md5", "")
    filename = body.get("filename", "")
    file_type = body.get("type", "")
    force = body.get("force", False)

    async with Session() as db:
        # 校验用户是否拥有该文件
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user, UserFileList.md5 == md5_val
            ).limit(1)
        )
        if result.scalar() is None:
            return {"code": 1, "msg": "file not found or no permission"}

        # 已有完成记录且非强制 → 直接返回（检查 chunk_index=0 即可）
        if not force:
            result = await db.execute(
                select(UserFileAiDesc).where(
                    UserFileAiDesc.user == user,
                    UserFileAiDesc.md5 == md5_val,
                    UserFileAiDesc.chunk_index == 0,
                    UserFileAiDesc.status == 1,
                ).limit(1)
            )
            if result.scalar():
                return {"code": 0, "msg": "already exists"}

        # 全局缓存命中 → 复制所有 chunk 到用户表
        if not force:
            result = await db.execute(
                select(FileAiDesc).where(
                    FileAiDesc.md5 == md5_val, FileAiDesc.status == 1
                ).order_by(FileAiDesc.chunk_index)
            )
            caches = result.scalars().all()
            if caches:
                return await _copy_cache_to_user(db, user, md5_val, caches)

        # 强制重建：先清掉旧切片记录（新旧分块数可能不同，避免残留过期切片）
        if force:
            await db.execute(
                delete(UserFileAiDesc).where(
                    UserFileAiDesc.user == user, UserFileAiDesc.md5 == md5_val
                )
            )
            # 旧切片向量仍在 FAISS 索引中，标记脏让下次搜索前自动重建，防止幽灵向量堆积
            mark_dirty(user)

        # 生成描述（返回 描述文本 + 结构化 metadata + 是否为降级存根）
        description, metadata, is_stub = await _generate_description(
            db, md5_val, filename, file_type, api_key, force=force
        )
        if not description:
            return {"code": 1, "msg": "describe failed"}

        # 分块 + 逐块向量化 + 存储（传入 metadata 供 PDF/Excel 切分器使用）
        chunks = chunk_text(file_type, description, metadata=metadata)
        if is_stub:
            # 降级存根不入语义索引：无内容信息，向量化只会污染搜索结果，
            # 也无需花费 embedding 调用。只落文本（供展示/手动重建入口判断），embedding 置空。
            for ch in chunks:
                ch.context_label = "未索引内容"
                await _upsert_file_ai_desc(db, md5_val, ch.text, None,
                                           ch.index, ch.context_label)
                await _upsert_user_ai_desc(db, user, md5_val, ch.text, None,
                                           ch.index, ch.context_label)
            await db.commit()
            if force:
                # force 重建为存根时同样需要清掉旧向量
                try:
                    mark_dirty(user)
                except Exception:
                    pass
            return {"code": 0, "msg": "ok"}

        vecs: list[np.ndarray] = []

        for ch in chunks:
            try:
                embedding = await get_embedding(api_key, ch.text)
            except RuntimeError as exc:
                return {"code": 1, "msg": f"embedding failed: {exc}"}

            vec = np.array(embedding, dtype=np.float32)
            vecs.append(vec)

            # 写入全局缓存（带 chunk_index + context_label）
            await _upsert_file_ai_desc(db, md5_val, ch.text, vec,
                                       ch.index, ch.context_label)
            # 写入用户记录
            await _upsert_user_ai_desc(db, user, md5_val, ch.text, vec,
                                       ch.index, ch.context_label)

        # FAISS 逐块写入；磁盘操作挪线程池避免阻塞事件循环。
        # 注意：必须先显式 flush 让 upsert 的行真正落库——
        # execute(text UPDATE) 不会触发 autoflush，否则回写 faiss_id 会因行不存在而静默丢失
        try:
            await db.flush()
            busy = False
            for i, ch in enumerate(chunks):
                faiss_id = await asyncio.to_thread(add_vector, user, vecs[i])
                if faiss_id < 0:
                    # 锁被重建占用（add_vector 非阻塞抢锁）：跳过回写保持 faiss_id=-1，
                    # 标脏让下次搜索前重建把该向量补进索引
                    busy = True
                    continue
                await db.execute(
                    text(
                        "UPDATE user_file_ai_desc SET faiss_id = :fid "
                        "WHERE user = :user AND md5 = :md5 AND chunk_index = :cidx"
                    ),
                    {"fid": faiss_id, "user": user, "md5": md5_val, "cidx": ch.index},
                )
            await db.commit()
            if busy:
                mark_dirty(user)
        except Exception:
            # 部分 add_vector 已入索引但回写/commit 失败：DB 回滚后索引里残留
            # 无 DB 行对应的向量，标脏让下次搜索前重建收敛，而不是留幽灵向量
            try:
                mark_dirty(user)
            except Exception:
                pass
            return {"code": 1, "msg": "faiss write failed"}

    # 强制重建后分块集合已变化：标记索引脏，下次搜索前自动按新记录重建
    if force:
        try:
            mark_dirty(user)
        except Exception:
            pass

    return {"code": 0, "msg": "ok"}


async def _generate_description(db, md5_val: str, filename: str, file_type: str,
                                api_key: str, force: bool = False) -> tuple[str, dict | None, bool]:
    """
    按文件类型生成中文描述，返回 (描述文本, 结构化metadata, 是否为降级存根)。
    metadata 供 chunker 使用（PDF 页面列表、Excel 工作表行等）。
    体积门槛（force 时放宽到硬上限）只做解析安全线；超过或提取失败时
    返回结构化存根（含"未索引内容"语义，前端可提示手动建立内容索引）。
    """
    ft = file_type.lower()

    # ── 图片：Qwen-VL 多模态描述（不涉及内容下载）──
    if ft in _IMAGE_TYPES:
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).limit(1)
        )
        fi = result.scalar()
        if fi and fi.url:
            public_url = _build_public_url(fi.url)
            desc = await describe_image(api_key, public_url)
            if not desc or desc == "无法描述此图片":
                # VL 失败时退化为结构化存根（打"未索引内容"标），
                # 避免占位文案被当作真实内容向量化入库
                return _make_stub_description(ft, filename, fi.size, "AI 视觉描述失败"), None, True
            return desc, None, False
        return f"图片文件：{filename}", None, False

    cap = _forced_cap() if force else _normal_cap()

    # ── PDF：返回页面列表 + 格式化描述 ──
    if ft == "pdf":
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).limit(1)
        )
        fi = result.scalar()
        if fi and fi.url:
            internal_url = _build_internal_url(fi.url)
            desc, meta, err = await _download_and_extract_pdf(internal_url, filename, cap)
            if desc:
                return desc, meta, False
            if err:
                return _make_stub_description(ft, filename, fi.size, err), None, True
        return f"PDF文件：{filename}", None, False

    # ── Excel：返回 sheet 行数据 + 格式化描述 ──
    if ft in ("xlsx", "xls"):
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).limit(1)
        )
        fi = result.scalar()
        if fi and fi.url:
            internal_url = _build_internal_url(fi.url)
            desc, meta, err = await _download_and_extract_xlsx(internal_url, filename, cap)
            if desc:
                return desc, meta, False
            if err:
                return _make_stub_description(ft, filename, fi.size, err), None, True
        return f"Excel文件：{filename}", None, False

    # ── 文本类 / docx：下载文件提取内容 ──
    if ft in _TEXT_TYPES or ft == "docx":
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).limit(1)
        )
        fi = result.scalar()
        if fi and fi.url:
            internal_url = _build_internal_url(fi.url)
            content, err = await _download_and_extract(internal_url, ft, cap)
            if content:
                return _format_text_description(filename, content), None, False
            if err:
                return _make_stub_description(ft, filename, fi.size, err), None, True

    # ── 其他：类型 + 文件名 ──
    return f"{file_type or '未知'}类型的文件：{filename}", None, False


# ── 支持的文件类型 ──

_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}

_TEXT_TYPES = {
    "txt", "md", "csv", "json", "xml", "html", "htm",
    "log", "c", "cpp", "h", "py", "js", "css", "java",
}


async def _download_and_extract(file_url: str, file_type: str, cap_bytes: int) -> tuple[str | None, str | None]:
    """
    下载文本/docx 文件并提取文本。
    返回 (文本, 失败原因|None)——失败原因为 _TOO_LARGE_MSG / _EXTRACT_FAIL_MSG，
    调用方据此生成降级存根。
    """
    try:
        data = await _download_capped(file_url, cap_bytes)
    except _DownloadTooLarge:
        return None, _TOO_LARGE_MSG
    except RuntimeError:
        return None, _EXTRACT_FAIL_MSG

    if file_type == "docx":
        text = await asyncio.to_thread(_parse_docx, data)
    else:
        text = await asyncio.to_thread(_decode_text, data)
    return text, None  # text 为 None 表示解析不出内容，由调用方走原兜底


def _parse_docx(data: bytes) -> str | None:
    """
    解压 docx（ZIP 格式），从 word/document.xml 中提取 <w:t> 标签内的文本。
    <w:t> 属性段必须限定"空格开头"（(?: [^>]*)?）：[^>]* 能匹配 < 和 /，
    会把无属性 <w:t>文本</w:t> 的闭合标签一起吞进属性段——文本被丢弃、
    捕获组跨段落抓到 XML 碎片（python-docx 与 Word 均大量输出无属性 w:t）。
    按段落提取、段落间以 \n 连接，避免相邻段落文字粘连。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except Exception:
        return None

    xml_text = _decode_text(xml_bytes)
    if not xml_text:
        return None

    paras = []
    # <w:p[ >] 排除 <w:pPr>/<w:pStyle>；<w:p/> 自闭合空段落自然跳过
    for p_match in re.finditer(r"<w:p[ >].*?</w:p>", xml_text, re.DOTALL):
        parts = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p_match.group())
        if parts:
            paras.append(html.unescape("".join(parts)))
    text = "\n".join(paras).strip()
    return text if text else None


def _decode_text(data: bytes) -> str | None:
    """尝试 UTF-8 解码，失败则用 latin-1 兜底"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except Exception:
            return None


def _format_text_description(filename: str, content: str) -> str:
    """格式化文本/文档描述：文件名 + 内容（截断到统一字符预算）"""
    budget = settings.extract_char_budget
    return f"文件名：{filename}\n文件内容：{content[:budget]}"


# ── PDF 文本提取 ──

async def _download_and_extract_pdf(file_url: str, filename: str, cap_bytes: int) -> tuple[str | None, dict | None, str | None]:
    """下载 PDF 并提取文本，返回 (描述, metadata, 失败原因|None)"""
    try:
        data = await _download_capped(file_url, cap_bytes)
    except _DownloadTooLarge:
        return None, None, _TOO_LARGE_MSG
    except RuntimeError:
        return None, None, _EXTRACT_FAIL_MSG
    desc, meta = await asyncio.to_thread(_extract_pdf_text, data, filename)
    return desc, meta, None


def _extract_pdf_text(data: bytes, filename: str) -> tuple[str, dict]:
    """
    从 PDF 二进制数据提取文本。
    返回 (描述文本, {"pages": [page_text, ...], "font_sizes": [size, ...]})。
    """
    meta: dict = {"pages": [], "font_sizes": []}
    if not data or len(data) == 0:
        return f"PDF文件（空文件）：{filename}", meta
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return f"PDF文件：{filename}", meta
    if doc.is_encrypted:
        doc.close()
        return f"PDF文件（已加密，无法提取文本）：{filename}", meta
    total = doc.page_count
    if total == 0:
        doc.close()
        return f"PDF文件（无页面）：{filename}", meta

    max_chars = settings.extract_char_budget
    text_parts: list[str] = []
    scanned = 0
    for i in range(total):
        try:
            page = doc.load_page(i)
        except Exception:
            meta["pages"].append(""); meta["font_sizes"].append(0); continue

        page_text = page.get_text("text").strip() if page else ""
        meta["pages"].append(page_text)
        # 提取字号用于突变检测
        try:
            blocks = page.get_text("dict").get("blocks", [])
            fs = 0
            for b in blocks:
                if b.get("type") == 0:
                    for ln in b.get("lines", []):
                        for sp in ln.get("spans", []):
                            fs = round(sp.get("size", 0)); break
                        if fs: break
                if fs: break
            meta["font_sizes"].append(fs)
        except Exception:
            meta["font_sizes"].append(0)

        if not page_text:
            try:
                if page.get_images(): scanned += 1
            except Exception:
                pass
            continue
        text_parts.append(page_text)
        if sum(len(t) for t in text_parts) > max_chars * 2:
            break
    doc.close()

    if not text_parts:
        if scanned > 0:
            return f"PDF文件（疑似扫描型，{scanned}/{total} 页无文本）：{filename}", meta
        return f"PDF文件（无可提取文本）：{filename}", meta

    raw = "\n".join(text_parts)
    raw = _clean_pdf_text(raw)
    if len(raw) > max_chars:
        raw = raw[:max_chars]
        last_period = raw.rfind("。")
        if last_period > max_chars // 2:
            raw = raw[:last_period + 1]
    note = f"（注：{scanned}/{total} 页为扫描页，已跳过）\n" if scanned > 0 else ""
    return f"文件名：{filename}\n{note}PDF内容：{raw}", meta


def _clean_pdf_text(text: str) -> str:
    """清洗 PDF 提取文本：去控制字符、合并空白、规整换行"""
    text = text.replace("\t", " ")
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


# ── Excel 表格提取 ──

async def _download_and_extract_xlsx(file_url: str, filename: str, cap_bytes: int) -> tuple[str | None, dict | None, str | None]:
    """下载 Excel 并提取文本，返回 (描述, {sheets}, 失败原因|None)"""
    try:
        data = await _download_capped(file_url, cap_bytes)
    except _DownloadTooLarge:
        return None, None, _TOO_LARGE_MSG
    except RuntimeError:
        return None, None, _EXTRACT_FAIL_MSG
    desc, meta = await asyncio.to_thread(_extract_xlsx_text, data, filename)
    return desc, meta, None


def _extract_xlsx_text(data: bytes, filename: str) -> tuple[str, dict]:
    """
    从 Excel 二进制数据提取文本。
    返回 (描述文本, {"sheets": [{"name": ..., "rows": [[...], ...]}, ...]})。
    """
    meta: dict = {"sheets": []}
    if not data or len(data) == 0:
        return f"Excel文件（空文件）：{filename}", meta
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return f"Excel文件（无法解析）：{filename}", meta

    max_chars = settings.extract_char_budget
    parts: list[str] = []
    sheet_count = 0
    empty_sheets = 0

    for name in wb.sheetnames:
        ws = wb[name]
        sheet_count += 1
        sheet_parts = [f"【{name}】"]
        sheet_rows: list[list[str]] = []
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            values = [str(v) if v is not None else "" for v in row]
            if not any(v for v in values):
                continue
            sheet_rows.append(values)
            sheet_parts.append("\t".join(values))
            row_count += 1
            total = sum(len(p) for p in parts) + sum(len(p) for p in sheet_parts)
            if total > max_chars * 2:
                break
        meta["sheets"].append({"name": name, "rows": sheet_rows})
        if row_count == 0:
            empty_sheets += 1
        else:
            parts.extend(sheet_parts)
    wb.close()

    if not parts:
        return f"Excel文件（{sheet_count} 个工作表均无数据）：{filename}", meta

    raw = "\n".join(parts)
    raw = _clean_pdf_text(raw)
    if len(raw) > max_chars:
        raw = raw[:max_chars]
        last_line = raw.rfind("\n")
        if last_line > max_chars // 2:
            raw = raw[:last_line]
    note = f"（共 {sheet_count} 个工作表）\n" if sheet_count > 1 else ""
    return f"文件名：{filename}\n{note}表格内容：\n{raw}", meta


def _build_internal_url(db_url: str) -> str:
    """
    构造后端内网自取文件的下载 URL（用于下载文本/docx/pdf/xlsx 内容）。
    桶已私有化，直接连 MinIO 获取需带签名；签出的 URL 即内网端点本身。
    db_url 格式: /files/{bucket}/{object_name}
    """
    return presign_internal_url(db_url_to_object(db_url))


async def _copy_cache_to_user(db, user: str, md5_val: str, caches: list) -> dict:
    """从全局缓存 file_ai_desc 复制所有 chunk 到用户表 user_file_ai_desc"""
    for cache in caches:
        if not cache.embedding:
            continue

        vec = np.frombuffer(cache.embedding, dtype=np.float32).copy()
        cidx = getattr(cache, "chunk_index", 0)
        clabel = getattr(cache, "context_label", "")

        await _upsert_user_ai_desc(db, user, md5_val, cache.description,
                                   vec, cidx, clabel)

        # 同 ai_describe：先 flush 落库，UPDATE 的 faiss_id 回写才能匹配到行
        await db.flush()
        faiss_id = await asyncio.to_thread(add_vector, user, vec)
        if faiss_id < 0:
            # 锁被重建占用：不回写 faiss_id（保持 -1），标脏待下次搜索前重建补齐
            mark_dirty(user)
        else:
            await db.execute(
                text(
                    "UPDATE user_file_ai_desc SET faiss_id = :fid "
                    "WHERE user = :user AND md5 = :md5 AND chunk_index = :cidx"
                ),
                {"fid": faiss_id, "user": user, "md5": md5_val, "cidx": cidx},
            )

    await db.commit()
    return {"code": 0, "msg": "ok"}


async def _upsert_file_ai_desc(db, md5_val: str, description: str, vec: np.ndarray | None,
                               chunk_index: int = 0, context_label: str = ""):
    """写入或更新全局 AI 描述缓存（按 md5 + chunk_index 唯一）；vec=None 表示存根行，不参与向量索引"""
    emb = vec.tobytes() if vec is not None else None
    result = await db.execute(
        select(FileAiDesc).where(
            FileAiDesc.md5 == md5_val, FileAiDesc.chunk_index == chunk_index
        ).limit(1)
    )
    existing = result.scalar()
    if existing:
        existing.description = description
        existing.embedding = emb
        existing.context_label = context_label
        existing.model = settings.vl_model
        existing.status = 1
    else:
        db.add(FileAiDesc(
            md5=md5_val, chunk_index=chunk_index,
            description=description, embedding=emb,
            context_label=context_label, model=settings.vl_model, status=1,
        ))


async def _upsert_user_ai_desc(db, user: str, md5_val: str, description: str,
                                vec: np.ndarray | None, chunk_index: int = 0,
                                context_label: str = ""):
    """写入或更新用户 AI 描述记录（按 user + md5 + chunk_index 唯一）"""
    result = await db.execute(
        select(UserFileAiDesc).where(
            UserFileAiDesc.user == user,
            UserFileAiDesc.md5 == md5_val,
            UserFileAiDesc.chunk_index == chunk_index,
        ).limit(1)
    )
    existing = result.scalar()
    if existing:
        existing.description = description
        if vec is not None:
            existing.embedding = vec.tobytes()
        existing.context_label = context_label
        existing.status = 1
    else:
        db.add(UserFileAiDesc(
            user=user, md5=md5_val, chunk_index=chunk_index,
            description=description,
            embedding=vec.tobytes() if vec is not None else None,
            context_label=context_label, status=1,
        ))


async def _rebuild_user_index(user: str) -> int:
    """
    全量重建用户 FAISS 索引（持锁快照 → 整体替换 → 回写 faiss_id），返回向量数。

    关键：DB 快照在持锁之后才读取。快照在锁外读的话，select 与 rebuild 之间
    并发 ai_describe 完成 add_vector（基于旧索引分配 id=k 写回 DB），重建随后
    用不含该行的快照整体替换索引——新向量丢失且 faiss_id=k 指向别的 chunk。
    持锁后 add_vector 无法插入（非阻塞抢锁必失败并 mark_dirty），快照因此
    与索引替换之间无并发写入。
    """
    lock_fd = await asyncio.to_thread(acquire_lock, user)  # 阻塞等待，排队串行重建
    if lock_fd < 0:
        raise RuntimeError("faiss index lock unavailable")
    try:
        async with Session() as db:
            result = await db.execute(
                select(UserFileAiDesc).where(
                    UserFileAiDesc.user == user,
                    UserFileAiDesc.status == 1,
                    UserFileAiDesc.embedding.isnot(None),
                ).order_by(UserFileAiDesc.id)
            )
            rows = result.scalars().all()

            vectors = [np.frombuffer(r.embedding, dtype=np.float32) for r in rows]
            await asyncio.to_thread(rebuild_locked, user, vectors)

            # 回写 faiss_id（重建后 id 从 0 重新分配）
            idx = await asyncio.to_thread(load_index, user)
            for i, r in enumerate(rows):
                if i < idx.ntotal:
                    r.faiss_id = i
            await db.commit()
            return len(rows)
    finally:
        release_lock(lock_fd)


async def _auto_rebuild_if_dirty(user: str):
    """
    脏标记检测 + 自动重建 FAISS 索引。
    文件删除后，dealfile 端点会创建 .dirty 文件，搜索前此处自动重建。
    锁在 _rebuild_user_index 内部获取（与 add_vector 互斥），此处不持锁调用。
    并发触发时最多重复一次幂等重建（数据源相同），不会写坏索引。
    """
    if not is_dirty(user):
        return

    started_at = time.time()
    await _rebuild_user_index(user)
    # 只清"重建开始前"的脏标记：重建期间并发 mark_dirty 的新标记保留，
    # 否则该次删除的幽灵向量要等到下一次删除才被清理
    await asyncio.to_thread(clear_dirty_if_older, user, started_at)


# ═══════════════════════════════════════════════════════════
#  查询规划 —— 一次 LLM 调用完成类型判断 / 改写 / 关键词生成
# ═══════════════════════════════════════════════════════════

# 规划结果约束（程序侧仅做结构校验，不做任何语义层面的类型判断）
_PLAN_MAX_QUERIES = 5          # vector_queries 条数上限
_PLAN_MAX_KEYWORDS = 8         # mysql_keywords 条数上限
_PLAN_MAX_QUERY_CHARS = 128    # 单条改写查询长度上限
_PLAN_MAX_KEYWORD_CHARS = 64   # 单个关键词长度上限

_PLAN_PROMPT = (
    "你是文件搜索系统的查询规划器。分析用户查询，判断查询类型并生成两路召回参数，"
    "只输出一个 JSON 对象，不要任何解释，不要代码块标记。\n"
    "输出格式：\n"
    '{{"query_type": "fuzzy 或 exact", "vector_queries": ["..."], "mysql_keywords": ["..."]}}\n'
    "判断规则：\n"
    "- 查询是对文件内容的模糊描述（如\"讲分布式存储的那份资料\"）→ query_type 为 fuzzy\n"
    "- 查询含精确锚点：完整或部分文件名、扩展名、日期、编号、版本号、专有名词"
    "（如\"季度报告Q3.xlsx\"）→ query_type 为 exact\n"
    "参数规则：\n"
    "- fuzzy：vector_queries 给 3 个同义改写查询；mysql_keywords 通常为空数组，"
    "仅当查询中确有适合文件名精确匹配的词时才给出\n"
    "- exact：mysql_keywords 给出文件名中可能原样出现的关键词"
    "（扩展名、日期、编号、专有名词等）；vector_queries 给 1 个查询（原始或规范化后）\n"
    "用户查询: {query}"
)


def _fallback_plan(query: str) -> dict:
    """LLM 规划失败/结果非法时的安全降级：原始查询走单条向量召回，不跑关键词路"""
    return {
        "query_type": "fuzzy",
        "vector_queries": [query],
        "mysql_keywords": [],
    }


def _clean_plan_list(value, max_items: int, max_chars: int) -> list[str]:
    """清洗字符串数组：去非字符串项、去空白、截断超长、去重，最多保留 max_items 条"""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    items: list[str] = []
    for v in value:
        if not isinstance(v, str):
            continue
        s = v.strip()[:max_chars]
        if s and s not in seen:
            seen.add(s)
            items.append(s)
        if len(items) >= max_items:
            break
    return items


def _parse_plan(raw: str, query: str) -> dict:
    """
    解析并校验 LLM 规划输出。程序只做结构检查：
    JSON 合法性、query_type 是否为允许值、数组与字符串长度约束。
    非法结果一律返回安全降级，不做语义层面的补救。
    """
    text = raw.strip()
    # 容错剥掉 ```json ... ``` 围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return _fallback_plan(query)
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return _fallback_plan(query)

    if not isinstance(data, dict) or data.get("query_type") not in ("fuzzy", "exact"):
        return _fallback_plan(query)

    vector_queries = _clean_plan_list(
        data.get("vector_queries"), _PLAN_MAX_QUERIES, _PLAN_MAX_QUERY_CHARS)
    mysql_keywords = _clean_plan_list(
        data.get("mysql_keywords"), _PLAN_MAX_KEYWORDS, _PLAN_MAX_KEYWORD_CHARS)

    # 向量路输入缺失时用原始查询兜底，保证语义召回能力不因规划缺字段而丢失；
    # mysql_keywords 为空是合法状态（调用方据此跳过关键词路）
    if not vector_queries:
        vector_queries = [query]

    return {
        "query_type": data["query_type"],
        "vector_queries": vector_queries,
        "mysql_keywords": mysql_keywords,
    }


async def plan_search_query(query: str, api_key: str) -> dict:
    """
    一次 LLM 调用完成查询规划：判断查询类型（fuzzy/exact）、生成向量改写查询
    vector_queries、生成 MySQL 关键词 mysql_keywords。
    LLM 网络失败/超时/返回非法 JSON 时安全降级为原始查询单条向量召回，
    不中断搜索链路。
    """
    try:
        client = create_client(api_key)
        resp = await client.chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "user", "content": _PLAN_PROMPT.format(query=query)}],
            max_tokens=300,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        logger.exception("LLM 查询规划调用失败，使用安全降级")
        return _fallback_plan(query)
    return _parse_plan(raw, query)


async def _embed_queries(queries: list[str], api_key: str, fallback_query: str,
                         timing: dict | None = None) -> np.ndarray:
    """
    对规划出的 vector_queries 逐条 embedding 后取均值并归一化（保留向量合并逻辑）。
    部分失败时用成功的那部分求均值；全部失败时退回对原始查询单独 embedding。
    """
    started = time.perf_counter()
    vecs: list[np.ndarray] = []
    for q in queries:
        try:
            emb = await get_embedding(api_key, q)
            vecs.append(np.array(emb, dtype=np.float32))
        except Exception:
            continue

    if not vecs:
        emb = await get_embedding(api_key, fallback_query)
        vec = np.array(emb, dtype=np.float32)
    else:
        vec = l2_normalize(np.mean(vecs, axis=0))

    if timing is not None:
        timing["embedding_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return vec


async def _keyword_search(user: str, keywords: list[str], query: str = "",
                           limit: int = 20) -> list[dict]:
    """
    MySQL 关键词模糊搜索，支持参数化查询防注入。
    三级排序：命中关键词数量 DESC → 文件名完全匹配优先 → 创建时间 DESC。
    """
    if not keywords:
        return []

    # 构建参数化 LIKE 条件
    like_clauses = []
    params: dict = {"user": user, "limit": limit}
    for i, kw in enumerate(keywords):
        key = f"kw{i}"
        like_clauses.append(f"ufl.file_name LIKE :{key}")
        params[key] = f"%{kw}%"

    # 完全匹配条件：文件名包含原始查询字符串
    exact_clause = "0"
    if query:
        params["exact_query"] = f"%{query}%"
        exact_clause = "CASE WHEN ufl.file_name LIKE :exact_query THEN 1 ELSE 0 END"

    # 命中计数
    hit_parts = " + ".join([f"(ufl.file_name LIKE :kw{i})" for i in range(len(keywords))])

    sql = (
        f"SELECT ufl.md5, ufl.file_name, fi.url, fi.size, fi.type, ufl.pv, "
        f"  ({hit_parts}) AS hit_count, {exact_clause} AS exact_match "
        f"FROM user_file_list ufl "
        f"JOIN file_info fi ON fi.md5 = ufl.md5 "
        f"WHERE ufl.user = :user AND ({' OR '.join(like_clauses)}) "
        f"ORDER BY hit_count DESC, exact_match DESC, ufl.create_time DESC "
        f"LIMIT :limit"
    )
    try:
        async with Session() as db:
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
            files = []
            for row in rows:
                files.append({
                    "md5": row[0], "filename": row[1], "url": presign_from_db_url(row[2]),
                    "size": str(row[3] or 0), "type": row[4], "pv": row[5],
                    "hit_count": row[6], "source": "keyword",
                })
            return files
    except Exception:
        return []


# ── LLM 重排序 ──

_RERANK_PROMPT = (
    "给定用户查询和候选文件列表，按与查询的相关性从高到低排序。\n"
    "只输出排名后的候选序号（从1开始），每行一个数字，不要解释。\n\n"
    "查询: {query}\n\n候选文件:\n{candidates}"
)


async def _rerank_with_llm(query: str, files: list[dict], api_key: str) -> list[dict]:
    """
    LLM 重排序：将 RRF Top-20 发送给 LLM 精排，返回 Top-10。
    失败时由调用方降级回退到 RRF 原始排名。
    """
    if len(files) <= 1:
        return files

    # 构建候选列表文本（最多 20 个）：文件名 + 描述 + 匹配上下文 + RRF 分数
    candidates_text = ""
    for i, f in enumerate(files[:20], 1):
        desc = (f.get("description") or "")[:80].replace("\n", " ")
        ctx = (f.get("match_context") or "")[:40].replace("\n", " ")
        candidates_text += (
            f"{i}. 文件名:{f['filename']} | 描述:{desc} | 位置:{ctx} | RRF:{f.get('rrf_score', 0)}\n"
        )

    prompt = _RERANK_PROMPT.format(query=query, candidates=candidates_text)

    client = create_client(api_key)
    resp = await client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=80,
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()

    # 解析排序序号
    indices = []
    for line in raw.split("\n"):
        try:
            idx = int(line.strip().rstrip("."))
            if 1 <= idx <= len(files):
                indices.append(idx - 1)
        except ValueError:
            continue

    if not indices:
        return files[:10]  # 解析失败 → 取 RRF 前十

    # 去重 + 补齐未出现的文件
    seen = set()
    result = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            result.append(files[idx])
    for i, f in enumerate(files):
        if i not in seen and len(result) < 10:
            result.append(f)

    return result[:10]


# ============================================================
#  search —— AI 语义搜索（一次查询规划 + 双路召回 + 统一 RRF）
#  独立搜索框与 /ai/search 端点已移除；本引擎由 AI 助手的
#  search_files 工具调用（agent_tools.tool_search_files）。
# ============================================================

async def _vector_lookup(user: str, valid: list[tuple[int, float]]) -> dict[str, dict]:
    """
    向量路 faiss_id → MySQL 联表批量回查（一次 IN 查询，替代逐 id 单查的 N+1）。
    返回 md5 → 文件信息（含 vector_rank / 余弦分 / match_context）；
    同 md5 多 chunk 命中保留最高分。结果取齐即退出会话——后续 RRF 融合与
    LLM 重排是纯内存/纯网络操作，不应占着连接池连接。
    """
    vec_ranked: dict[str, dict] = {}
    async with Session() as db:
        db_rows = (await db.execute(
            select(UserFileAiDesc, UserFileList, FileInfo)
            .join(UserFileList,
                  (UserFileList.user == UserFileAiDesc.user) &
                  (UserFileList.md5 == UserFileAiDesc.md5))
            .join(FileInfo, FileInfo.md5 == UserFileAiDesc.md5)
            .where(
                UserFileAiDesc.user == user,
                UserFileAiDesc.faiss_id.in_([fid for fid, _ in valid]),
                UserFileAiDesc.status == 1,
            )
        )).all()
        by_fid = {uad.faiss_id: (uad, ufl, fi) for uad, ufl, fi in db_rows}

        # 只有通过阈值的结果占用名次——被跳过项继续计数会稀释后续项的 RRF 排名
        rank = 0
        for fid, score in valid:
            row = by_fid.get(fid)
            if not row:
                continue
            rank += 1
            uad, ufl, fi = row
            md5 = uad.md5
            if md5 not in vec_ranked or score > vec_ranked[md5]["score"]:
                vec_ranked[md5] = {
                    "md5": md5,
                    "filename": ufl.file_name,
                    "description": uad.description,
                    "url": presign_from_db_url(fi.url), "size": str(fi.size), "type": fi.type,
                    "score": round(score, 4),
                    "vector_rank": rank,
                    "match_context": uad.context_label or "",
                    "chunk_index": uad.chunk_index,
                }
    return vec_ranked


def _merge_rrf(vec_ranked: dict[str, dict], kw_files: list[dict], k: int = 60) -> list[dict]:
    """
    双路统一合并 + RRF 排序（纯内存，无 DB 会话）：
    - 以 md5 为唯一候选键，vector_rank / keyword_rank 各自可空；
    - 两路都有 → 1/(k+vector_rank) + 1/(k+keyword_rank)；单路 → 该路贡献；
    - source 仅作调试与展示，不参与排序；按 rrf_score 降序返回。
    """
    candidates: dict[str, dict] = {}

    # 向量路入表
    for md5, info in vec_ranked.items():
        info["keyword_rank"] = None
        info["source"] = "向量路"
        candidates[md5] = info

    # 关键词路入表 / 合并
    for rank_j, kf in enumerate(kw_files, start=1):
        md5 = kf["md5"]
        cand = candidates.get(md5)
        if cand is not None:
            cand["keyword_rank"] = rank_j
            cand["source"] = "双路混合"
        else:
            kf["vector_rank"] = None
            kf["keyword_rank"] = rank_j
            kf["description"] = ""
            kf["match_context"] = ""
            kf["score"] = 0.0
            kf["source"] = "关键词路"
            candidates[md5] = kf

    # 统一 RRF 分数
    for cand in candidates.values():
        s = 0.0
        if cand.get("vector_rank") is not None:
            s += 1.0 / (k + cand["vector_rank"])
        if cand.get("keyword_rank") is not None:
            s += 1.0 / (k + cand["keyword_rank"])
        cand["rrf_score"] = round(s, 5)

    return sorted(candidates.values(), key=lambda f: f["rrf_score"], reverse=True)


async def semantic_search(user: str, query: str, top_k: int = 10,
                          api_key: str | None = None) -> dict:
    """
    语义搜索全链路（全站唯一检索实现）：
    脏索引自动重建 → 一次 LLM 查询规划（类型判断 + 改写 + 关键词生成）→
    FAISS 向量召回 → MySQL 关键词召回 → 按 md5 合并的统一 RRF 排序 →
    候选数 >5 时 LLM 重排序。
    返回 {"code": 0, "count": n, "files": [...]} 或 {"code": 1, "msg": ...}
    """
    search_started = time.perf_counter()
    timing: dict[str, float] = {}

    # 密钥统一取服务端配置（客户端传入的 api_key 一律忽略，防泄露）
    if api_key is None:
        api_key = settings.dashscope_api_key

    if not query:
        return {"code": 1, "msg": "missing query"}
    if not api_key:
        return {"code": 1, "msg": "AI 服务未配置，请联系管理员"}

    # 脏标记检测 → 自动重建索引（文件删除后触发，保持现状）
    dirty_started = time.perf_counter()
    if is_dirty(user):
        try:
            await _auto_rebuild_if_dirty(user)
        except Exception:
            logger.exception("搜索前自动重建索引失败，用现存索引继续")
            pass  # 重建失败不影响搜索，用现存索引继续
    timing["dirty_index_ms"] = round((time.perf_counter() - dirty_started) * 1000, 1)

    # ── 一次 LLM 查询规划：类型判断 + 向量改写 + 关键词生成 ──
    # 查询类型与两路输入完全由 LLM 决定，程序只校验结构后按返回值执行
    plan_started = time.perf_counter()
    plan = await plan_search_query(query, api_key)
    timing["query_plan_ms"] = round((time.perf_counter() - plan_started) * 1000, 1)
    vector_queries = plan["vector_queries"]
    keywords = plan["mysql_keywords"]

    index_ready = await asyncio.to_thread(get_ntotal, user) > 0

    # ── 向量路（索引为空时整体跳过：无向量可搜，也省掉 embedding 调用）──
    results: list[tuple[int, float]] = []
    if index_ready:
        try:
            query_vec = await _embed_queries(vector_queries, api_key, query, timing)
        except RuntimeError as exc:
            return {"code": 1, "msg": f"embedding failed: {exc}"}
        try:
            faiss_started = time.perf_counter()
            results = await asyncio.to_thread(faiss_search, user, query_vec, top_k)
            timing["faiss_ms"] = round((time.perf_counter() - faiss_started) * 1000, 1)
        except Exception as exc:
            return {"code": 1, "msg": f"faiss search failed: {exc}"}
    else:
        timing["embedding_ms"] = 0.0
        timing["faiss_ms"] = 0.0

    # ── 关键词路：mysql_keywords 为空即跳过（输入由 LLM 规划给出）──
    # 向量路不可用时（索引为空：新用户、或尚未对任何文件建立 AI 描述），
    # 关键词路是唯一可用路径——用原始查询兜底，否则按文件名精确搜索
    # 也返回"没有找到"。
    if not keywords and not index_ready:
        fallback_kw = query.strip()[:64]
        if fallback_kw:
            keywords = [fallback_kw]
    mysql_keyword_started = time.perf_counter()
    kw_files = await _keyword_search(user, keywords, query=query, limit=top_k) if keywords else []
    timing["mysql_keyword_ms"] = round((time.perf_counter() - mysql_keyword_started) * 1000, 1)

    # ── 向量路候选：faiss_id 批量回查 MySQL（md5 去重取最高分）──
    valid = [(fid, score) for fid, score in results if score >= SCORE_THRESHOLD]
    mysql_vector_started = time.perf_counter()
    vec_ranked = await _vector_lookup(user, valid) if valid else {}
    timing["mysql_vector_join_ms"] = round((time.perf_counter() - mysql_vector_started) * 1000, 1)

    # ── 统一合并 + RRF 排序（md5 唯一键；source 仅展示）──
    rrf_started = time.perf_counter()
    files = _merge_rrf(vec_ranked, kw_files)
    timing["rrf_ms"] = round((time.perf_counter() - rrf_started) * 1000, 1)

    # ── LLM 重排：候选 ≤5 直接按 RRF 返回（省一次秒级 LLM 调用）；
    #    >5 取前 20 精排；重排失败回退 RRF 顺序 ──
    if len(files) > 5:
        rerank_started = time.perf_counter()
        try:
            files = await _rerank_with_llm(query, files[:20], api_key)
        except Exception:
            logger.exception("LLM 重排失败，回退统一 RRF 排序")
        timing["llm_rerank_ms"] = round((time.perf_counter() - rerank_started) * 1000, 1)
    else:
        timing["llm_rerank_ms"] = 0.0

    timing["total_ms"] = round((time.perf_counter() - search_started) * 1000, 1)
    logger.info(
        "SEARCH_TIMING user=%s query=%r qtype=%s vec_q=%d kw=%d timing=%s",
        user, query, plan["query_type"], len(vector_queries), len(keywords), timing,
    )

    return {"code": 0, "count": len(files), "files": files}


# ============================================================
#  rebuild —— 重建 FAISS 索引
# ============================================================

@router.post("/ai/rebuild")
async def ai_rebuild(body: dict):
    """
    从 MySQL 中读取用户所有已完成的 AI 描述向量，全量重建 FAISS 索引。
    需要: user, token
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    try:
        started_at = time.time()
        count = await _rebuild_user_index(user)
    except Exception as exc:
        return {"code": 1, "msg": f"faiss rebuild failed: {exc}"}

    # 手动重建成功后清掉残留脏标记（按 mtime 只清重建开始前的），
    # 否则下次搜索会再做一次冗余的自动重建
    await asyncio.to_thread(clear_dirty_if_older, user, started_at)

    return {"code": 0, "msg": "rebuilt", "count": count}
