"""
向量化任务执行管线 —— 下载/提取 → AI 描述 → 分块 → 逐块向量化 → 落库入索引

本模块的提取/描述/落库函数自 routers/ai.py 原样移植（行为不变），归入
indexing 任务域：HTTP 层只投递任务（见 scheduler.enqueue），重活全部在
这里由消费者（worker.py）异步执行，不再占用上传请求。

入口：run_indexing_job(job) -> (ok, error)
"""
import asyncio
import html
import io
import logging
import re
import zipfile

import fitz  # PyMuPDF
import httpx
import numpy as np
import openpyxl
from sqlalchemy import select, text, delete

from app.ai_usage import KIND_EMBEDDING, KIND_VL, make_row, write_usage
from app.chunker import chunk_text
from app.config import settings
from app.dashscope_client import describe_image, get_embedding
from app.database import Session
from app.faiss_service import add_vector, mark_dirty
from app.indexing.models import IndexingJob
from app.minio_client import presign_url, presign_internal_url, db_url_to_object
from app.models import FileInfo, FileAiDesc, UserFileAiDesc

logger = logging.getLogger("uvicorn.error")

# ── 内容提取三层策略（与搜索侧共享的口径：安全线→字符预算→存根降级）──

_TOO_LARGE_MSG = "文件超过解析上限"
_EXTRACT_FAIL_MSG = "内容提取失败"

_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}

_TEXT_TYPES = {
    "txt", "md", "csv", "json", "xml", "html", "htm",
    "log", "c", "cpp", "h", "py", "js", "css", "java",
}


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
    """供 DashScope（Qwen-VL）下载图片的公网 URL（桶私有，必须预签名）"""
    return presign_url(db_url_to_object(db_url), expires=3600)


def _build_internal_url(db_url: str) -> str:
    """构造后端内网自取文件的下载 URL（db_url 格式: /files/{bucket}/{object}）"""
    return presign_internal_url(db_url_to_object(db_url))


# ── 文本 / docx 提取 ──

async def _download_and_extract(file_url: str, file_type: str, cap_bytes: int) -> tuple[str | None, str | None, str | None]:
    """
    下载文本/docx 文件并提取文本。
    返回 (文本, 编码名|None, 失败原因|None)——编码供描述截断标记换算
    续读字节偏移；失败原因为 _TOO_LARGE_MSG / _EXTRACT_FAIL_MSG，
    调用方据此生成降级存根。
    """
    try:
        data = await _download_capped(file_url, cap_bytes)
    except _DownloadTooLarge:
        return None, None, _TOO_LARGE_MSG
    except RuntimeError:
        return None, None, _EXTRACT_FAIL_MSG

    if file_type == "docx":
        text = await asyncio.to_thread(_parse_docx, data)
        return text, None, None  # docx 提取文本与原始字节无对应，不携带编码
    text, encoding = await asyncio.to_thread(_decode_text, data)
    return text, encoding, None


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

    xml_text, _ = _decode_text(xml_bytes)
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


def _decode_text(data: bytes) -> tuple[str | None, str | None]:
    """尝试 UTF-8 解码，失败则用 latin-1 兜底。返回 (文本, 编码名|None)。

    编码名供调用方换算字节偏移：read_file 的 offset 是字节语义，描述截断
    标记需要给出精确续读位置（utf-8 取截断前缀的编码字节数，latin-1
    一字符一字节）。
    """
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1"), "latin-1"
        except Exception:
            return None, None


def _format_text_description(filename: str, content: str, encoding: str | None = None,
                             allow_read_file: bool = True) -> str:
    """格式化文本/文档描述：文件名 + 内容（截断到统一字符预算）。

    超过预算时追加截断标记，消除"前 3 万字符被 LLM 当作全文"的静默截断：
    文本类文件内嵌续读字节偏移（read_file 的 offset 语义，按实际编码精确
    换算）；docx 的提取文本与原始字节无对应关系，只标注截断不给偏移。
    """
    budget = settings.extract_char_budget
    if len(content) <= budget:
        return f"文件名：{filename}\n文件内容：{content}"
    prefix = content[:budget]
    if allow_read_file:
        byte_offset = len(prefix) if encoding == "latin-1" else len(prefix.encode("utf-8"))
        marker = (f"\n[注：以上为文件前 {budget} 字符，全文共 {len(content)} 字符，"
                  f"续读请调用 read_file 并传 offset={byte_offset}]")
    else:
        marker = f"\n[注：以上为文件前 {budget} 字符，全文共 {len(content)} 字符]"
    return f"文件名：{filename}\n文件内容：{prefix}{marker}"


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


# ── AI 描述生成 ──

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
            content, encoding, err = await _download_and_extract(internal_url, ft, cap)
            if content:
                # docx 提取文本与原始字节无对应关系，标记中不给 read_file 续读偏移
                return _format_text_description(
                    filename, content, encoding, allow_read_file=ft in _TEXT_TYPES,
                ), None, False
            if err:
                return _make_stub_description(ft, filename, fi.size, err), None, True

    # ── 其他：类型 + 文件名 ──
    return f"{file_type or '未知'}类型的文件：{filename}", None, False


# ── 落库辅助 ──

async def _copy_cache_to_user(db, user: str, md5_val: str, caches: list) -> None:
    """从全局缓存 file_ai_desc 复制所有 chunk 到用户表 user_file_ai_desc"""
    for cache in caches:
        if not cache.embedding:
            continue

        vec = np.frombuffer(cache.embedding, dtype=np.float32).copy()
        cidx = getattr(cache, "chunk_index", 0)
        clabel = getattr(cache, "context_label", "")

        await _upsert_user_ai_desc(db, user, md5_val, cache.description,
                                   vec, cidx, clabel)

        # 先 flush 落库，UPDATE 的 faiss_id 回写才能匹配到行
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


# ── 任务入口 ──

async def run_indexing_job(job: IndexingJob) -> tuple[bool, str]:
    """
    执行一个向量化任务（消费者调用）。返回 (ok, error)。

    与原 /ai/describe 请求内执行的逻辑一致，仅去掉 HTTP 语义：
    缓存命中复制 → 强制清理旧切片 → 描述生成 → 分块 → 逐块向量化 →
    FAISS 写入 → 记账。任何失败返回 False + 摘要，由调度器决定重试。
    """
    api_key = settings.dashscope_api_key
    if not api_key:
        return False, "AI service not configured"

    user, md5_val = job.user, job.md5
    filename, file_type = job.filename or "", (job.file_type or "").lower()
    force = bool(job.force)
    usage_rows: list[dict] = []

    async with Session() as db:
        # 全局缓存命中（非 force）→ 复制所有 chunk 到用户表即完成任务
        if not force:
            caches = (await db.execute(
                select(FileAiDesc).where(
                    FileAiDesc.md5 == md5_val, FileAiDesc.status == 1
                ).order_by(FileAiDesc.chunk_index)
            )).scalars().all()
            if caches:
                await _copy_cache_to_user(db, user, md5_val, caches)
                return True, ""

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
            return False, "describe failed"

        if file_type.lower() in _IMAGE_TYPES and not is_stub:
            usage_rows.append(make_row(user, KIND_VL, settings.vl_model))

        # 分块 + 逐块向量化 + 存储（传入 metadata 供 PDF/Excel 切分器使用）
        chunks = chunk_text(file_type, description, metadata=metadata)
        if is_stub:
            # 降级存根不入语义索引：无内容信息，向量化只会污染搜索结果，
            # 也无需花费 embedding 调用。只落文本，embedding 置空。
            for ch in chunks:
                ch.context_label = "未索引内容"
                await _upsert_file_ai_desc(db, md5_val, ch.text, None,
                                           ch.index, ch.context_label)
                await _upsert_user_ai_desc(db, user, md5_val, ch.text, None,
                                           ch.index, ch.context_label)
            await db.commit()
            if force:
                try:
                    mark_dirty(user)
                except Exception:
                    pass
            await write_usage(usage_rows)
            return True, ""

        vecs: list[np.ndarray] = []

        for ch in chunks:
            try:
                embedding = await get_embedding(api_key, ch.text)
            except RuntimeError as exc:
                return False, f"embedding failed: {exc}"

            vec = np.array(embedding, dtype=np.float32)
            vecs.append(vec)

            # 写入全局缓存（带 chunk_index + context_label）
            await _upsert_file_ai_desc(db, md5_val, ch.text, vec,
                                       ch.index, ch.context_label)
            # 写入用户记录
            await _upsert_user_ai_desc(db, user, md5_val, ch.text, vec,
                                       ch.index, ch.context_label)

        if vecs:
            # embedding 按块聚合记一次（API 不返回 token 数，记调用次数）
            usage_rows.append(make_row(
                user, KIND_EMBEDDING, settings.embedding_model, calls=len(vecs)))

        # FAISS 逐块写入；磁盘操作挪线程池避免阻塞事件循环。
        # 必须先显式 flush 让 upsert 的行真正落库——execute(text UPDATE)
        # 不会触发 autoflush，否则回写 faiss_id 会因行不存在而静默丢失
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
            if busy or force:
                # force 重建后分块集合已变化：标记索引脏，下次搜索前自动按新记录重建
                try:
                    mark_dirty(user)
                except Exception:
                    pass
        except Exception:
            # 部分 add_vector 已入索引但回写/commit 失败：DB 回滚后索引里残留
            # 无 DB 行对应的向量，标脏让下次搜索前重建收敛，而不是留幽灵向量
            try:
                mark_dirty(user)
            except Exception:
                pass
            return False, "faiss write failed"

    await write_usage(usage_rows)
    return True, ""
