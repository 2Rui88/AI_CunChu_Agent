"""
文件语义分块器 —— 按文件类型自适应切分长文本。

每种文件类型有独立的切分策略，利用文件原生的结构边界（段落、标题、
函数声明、页面等）作为切分点，embedding 模型 token 上限只做安全兜底。

架构：策略模式
  - BaseChunker    —— 抽象基类，定义统一接口
  - FallbackChunker —— 段落切分，适用所有文本类文件
  - MarkdownChunker —— ## 标题段落切分
  - CodeChunker    —— 函数/类声明切分
  - PDFChunker     —— 页面 + 字号突变检测
  - ExcelChunker   —— 工作表 + 数据行切分
  - DocxChunker    —— 标题样式段落切分

使用入口：chunk_text(file_type, text, max_chars) -> list[Chunk]
"""

import re
from dataclasses import dataclass
from typing import Optional

from app.config import settings

# 默认分块字符数
_DEFAULT_MAX_CHARS = 3000


@dataclass
class Chunk:
    """一个语义切片"""
    index: int
    text: str
    context_label: str


class BaseChunker:
    """分块器抽象基类"""

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════
#  FallbackChunker —— 通用段落切分
# ═══════════════════════════════════════════════════════════

class FallbackChunker(BaseChunker):
    """
    通用段落切分器 —— 以连续空行为边界切分。
    适用：纯文本及所有未注册策略的类型。
    """

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        paragraphs = self._split_paragraphs(text)
        merged = self._merge_paragraphs(paragraphs, max_chars)
        return self._build_chunks(merged, max_chars)

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        raw = re.split(r"\n{2,}", text)
        return [p.strip() for p in raw if p.strip()]

    @staticmethod
    def _merge_paragraphs(paragraphs: list[str], max_chars: int) -> list[str]:
        merged: list[str] = []
        buf: list[str] = []
        for para in paragraphs:
            current_len = sum(len(p) for p in buf) + len(buf)
            if current_len + len(para) <= max_chars:
                buf.append(para)
            else:
                if buf:
                    merged.append("\n\n".join(buf))
                buf = [para]
        if buf:
            merged.append("\n\n".join(buf))
        return merged

    @staticmethod
    def _split_oversized(block: str, max_chars: int) -> list[str]:
        parts = re.split(r"(?<=[。！？!?])(?=\S)", block)
        if len(parts) == 1:
            return [block[i:i + max_chars] for i in range(0, len(block), max_chars)]
        result: list[str] = []
        buf: list[str] = []
        buf_len = 0
        for part in parts:
            if buf_len + len(part) <= max_chars:
                buf.append(part)
                buf_len += len(part)
            else:
                if buf:
                    result.append("".join(buf))
                if len(part) > max_chars:
                    for i in range(0, len(part), max_chars):
                        result.append(part[i:i + max_chars])
                    buf, buf_len = [], 0
                else:
                    buf, buf_len = [part], len(part)
        if buf:
            result.append("".join(buf))
        return result

    @staticmethod
    def _build_chunks(merged_blocks: list[str], max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        for block in merged_blocks:
            if len(block) <= max_chars:
                label = FallbackChunker._make_label(block)
                chunks.append(Chunk(index=len(chunks), text=block, context_label=label))
            else:
                sub = FallbackChunker._split_oversized(block, max_chars)
                label = FallbackChunker._make_label(block)
                for part in sub:
                    chunks.append(Chunk(index=len(chunks), text=part, context_label=label))
        return chunks

    @staticmethod
    def _make_label(text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        match = re.search(r"[。！？.!?\n]", stripped)
        first = stripped[:match.start()] if match else stripped
        if len(first) > 30:
            first = first[:30] + "…"
        return first


# ═══════════════════════════════════════════════════════════
#  MarkdownChunker —— ## 标题段落切分
# ═══════════════════════════════════════════════════════════

class MarkdownChunker(BaseChunker):
    """
    Markdown 标题切分器 —— 以 ## 级标题为边界切分。
    """

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        sections = self._split_by_headings(text)
        return self._build_chunks(sections, max_chars)

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"^##\s+.+$", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if not matches:
            return [("", text)]
        sections: list[tuple[str, str]] = []
        if matches[0].start() > 0:
            before = text[:matches[0].start()].strip()
            if before:
                sections.append(("", before))
        for i, m in enumerate(matches):
            label = m.group().strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            if content:
                sections.append((label, content))
        return sections

    def _build_chunks(self, sections: list[tuple[str, str]],
                      max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        for label, content in sections:
            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content, context_label=label))
            else:
                sub = self._split_by_subheadings(content, label)
                for sub_label, sub_content in sub:
                    if len(sub_content) <= max_chars:
                        chunks.append(Chunk(index=len(chunks), text=sub_content,
                                            context_label=sub_label or label))
                    else:
                        fb = FallbackChunker()
                        for sc in fb.chunk(sub_content, max_chars):
                            sc.context_label = sub_label or label
                            sc.index = len(chunks)
                            chunks.append(sc)
        return chunks

    @staticmethod
    def _split_by_subheadings(content: str, parent_label: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"^###\s+.+$", re.MULTILINE)
        matches = list(pattern.finditer(content))
        if not matches:
            return [(parent_label, content)]
        sections: list[tuple[str, str]] = []
        if matches[0].start() > 0:
            before = content[:matches[0].start()].strip()
            if before:
                sections.append((parent_label, before))
        for i, m in enumerate(matches):
            sub_label = m.group().strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sub_content = content[start:end].strip()
            if sub_content:
                sections.append((sub_label, sub_content))
        return sections


# ═══════════════════════════════════════════════════════════
#  CodeChunker —— 函数/类声明切分
# ═══════════════════════════════════════════════════════════

class CodeChunker(BaseChunker):
    """
    代码函数/类切分器 —— 以 def/function/class/fn/func 为边界。
    适用：.py .js .ts .java .c .cpp .h .go .rs 等。
    """

    _FUNC_CLASS_PATTERN = re.compile(
        r"^(\s*)"
        r"(?:(?:async\s+)?def\s+(\w+)"
        r"|function\s+(\w+)"
        r"|(?:public\s+|private\s+|protected\s+|static\s+)*"
        r"(?:class|interface)\s+(\w+)"
        r"|fn\s+(\w+)"
        r"|func\s+(\w+)"
        r"|(?:void|int|char|float|double|long|short|bool|auto|"
        r"string|String)\s+(\w+)\s*\([^)]*\)\s*\{?"
        r")",
        re.MULTILINE,
    )

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        matches = list(self._FUNC_CLASS_PATTERN.finditer(text))
        if len(matches) < 2:
            label = self._first_func_name(matches) if matches else ""
            return self._oversized_chunks(text, label, max_chars)

        chunks: list[Chunk] = []
        first = matches[0]
        if first.start() > 0:
            before = text[:first.start()].strip()
            if before:
                chunks.append(Chunk(index=0, text=before, context_label="文件头"))

        for i, m in enumerate(matches):
            name = self._extract_name(m)
            label = self._make_func_label(m.group(), name)
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            if not content:
                continue
            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content, context_label=label))
            else:
                for sc in self._oversized_chunks(content, label, max_chars):
                    sc.index = len(chunks)
                    chunks.append(sc)
        return chunks if chunks else self._oversized_chunks(text, "", max_chars)

    @staticmethod
    def _first_func_name(matches: list) -> str:
        return CodeChunker._extract_name(matches[0])

    @staticmethod
    def _extract_name(match: re.Match) -> str:
        for g in match.groups()[1:]:
            if g and g.strip():
                return g.strip()
        return ""

    @staticmethod
    def _make_func_label(raw_decl: str, name: str) -> str:
        return name if name else raw_decl.strip()[:40]

    @staticmethod
    def _oversized_chunks(text: str, label: str, max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        for i in range(0, len(text), max_chars):
            chunks.append(Chunk(index=len(chunks), text=text[i:i + max_chars], context_label=label))
        return chunks


# ═══════════════════════════════════════════════════════════
#  PDFChunker —— 页面 + 字号突变检测
# ═══════════════════════════════════════════════════════════

class PDFChunker(BaseChunker):
    """PDF 页面切分器。优先使用 metadata["pages"] 逐页文本列表。"""

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        pages = (metadata or {}).get("pages", [])
        font_sizes = (metadata or {}).get("font_sizes", [])
        if pages:
            return self._chunk_by_pages(pages, font_sizes, max_chars)
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]
        return FallbackChunker().chunk(text, max_chars)

    @staticmethod
    def _chunk_by_pages(pages: list[str], font_sizes: list, max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        buf_pages: list[int] = []
        buf_text: list[str] = []
        buf_len = 0
        for i, page_text in enumerate(pages):
            text_i = page_text.strip()
            if not text_i:
                continue
            if i < len(font_sizes) and buf_pages:
                last = font_sizes[buf_pages[-1]] if buf_pages[-1] < len(font_sizes) else 0
                curr = font_sizes[i]
                if last and curr and abs(last - curr) > 4:
                    label = PDFChunker._page_range_label(buf_pages)
                    chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text), context_label=label))
                    buf_pages, buf_text, buf_len = [], [], 0
            new_len = buf_len + len(text_i) + (1 if buf_text else 0)
            if new_len <= max_chars:
                buf_pages.append(i)
                buf_text.append(text_i)
                buf_len = new_len
            else:
                if buf_text:
                    label = PDFChunker._page_range_label(buf_pages)
                    chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text), context_label=label))
                if len(text_i) > max_chars:
                    label = PDFChunker._page_range_label([i])
                    chunks.append(Chunk(index=len(chunks), text=text_i[:max_chars], context_label=label))
                    buf_pages, buf_text, buf_len = [], [], 0
                else:
                    buf_pages, buf_text, buf_len = [i], [text_i], len(text_i)
        if buf_text:
            label = PDFChunker._page_range_label(buf_pages)
            chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text), context_label=label))
        return chunks

    @staticmethod
    def _page_range_label(pages: list[int]) -> str:
        if len(pages) == 1:
            return f"第{pages[0] + 1}页"
        return f"第{pages[0] + 1}-{pages[-1] + 1}页"


# ═══════════════════════════════════════════════════════════
#  ExcelChunker —— 工作表 + 数据行切分
# ═══════════════════════════════════════════════════════════

class ExcelChunker(BaseChunker):
    """Excel 工作表切分器。优先使用 metadata["sheets"]。"""

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        sheets = (metadata or {}).get("sheets", [])
        if not sheets:
            return FallbackChunker().chunk(text, max_chars)
        chunks: list[Chunk] = []
        for sheet in sheets:
            name = sheet.get("name", "")
            rows = sheet.get("rows", [])
            if not rows:
                continue
            for sc in self._chunk_rows(rows, name, max_chars):
                sc.index = len(chunks)
                chunks.append(sc)
        return chunks if chunks else [Chunk(index=0, text=text or "", context_label="")]

    @staticmethod
    def _chunk_rows(rows: list[list[str]], sheet_name: str, max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        buf_rows: list[int] = []
        buf_text: list[str] = []
        buf_len = 0
        for i, row in enumerate(rows):
            row_str = "\t".join(str(v) if v is not None else "" for v in row)
            if not row_str.strip():
                continue
            new_len = buf_len + len(row_str) + (1 if buf_text else 0)
            if new_len <= max_chars:
                buf_rows.append(i)
                buf_text.append(row_str)
                buf_len = new_len
            else:
                if buf_text:
                    label = ExcelChunker._row_label(sheet_name, buf_rows)
                    chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text), context_label=label))
                if len(row_str) > max_chars:
                    label = ExcelChunker._row_label(sheet_name, [i])
                    chunks.append(Chunk(index=len(chunks), text=row_str[:max_chars], context_label=label))
                    buf_rows, buf_text, buf_len = [], [], 0
                else:
                    buf_rows, buf_text, buf_len = [i], [row_str], len(row_str)
        if buf_text:
            label = ExcelChunker._row_label(sheet_name, buf_rows)
            chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text), context_label=label))
        return chunks

    @staticmethod
    def _row_label(sheet_name: str, row_indices: list[int]) -> str:
        if len(row_indices) == 1:
            return f"{sheet_name} (行{row_indices[0] + 1})"
        return f"{sheet_name} (行{row_indices[0] + 1}-{row_indices[-1] + 1})"


# ═══════════════════════════════════════════════════════════
#  DocxChunker —— 标题样式段落切分
# ═══════════════════════════════════════════════════════════

class DocxChunker(BaseChunker):
    """
    docx 标题样式切分器 —— 以 Word 标题段落为边界。
    支持 metadata["headings"] 传入标题位置信息，或文本中的标题标记行。
    """

    _HEADING_MARKER = re.compile(r"^(?:Heading\s*\d+|第[一二三四五六七八九十]+[章节部])\b")

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        # metadata 中的标题位置信息
        headings = (metadata or {}).get("headings", [])
        if headings:
            return self._chunk_by_heading_meta(text, headings, max_chars)

        # 文本中的标题标记
        sections = self._split_by_markers(text)
        if len(sections) > 1:
            return self._build_chunks_from_sections(sections, max_chars)

        return FallbackChunker().chunk(text, max_chars)

    def _chunk_by_heading_meta(self, text: str, headings: list[dict],
                                max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        for i, h in enumerate(headings):
            start = h["pos"]
            end = headings[i + 1]["pos"] if i + 1 < len(headings) else len(text)
            content = text[start:end].strip()
            if not content:
                continue
            label = h.get("text", "")
            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content, context_label=label))
            else:
                for sc in self._split_oversized_headed(content, label, max_chars):
                    sc.index = len(chunks)
                    chunks.append(sc)
        return chunks

    @staticmethod
    def _split_by_markers(text: str) -> list[tuple[str, str]]:
        lines = text.splitlines()
        if not lines:
            return []
        sections: list[tuple[str, str]] = []
        buf_label = ""
        buf_lines: list[str] = []
        marker = DocxChunker._HEADING_MARKER
        for line in lines:
            stripped = line.strip()
            if marker.match(stripped):
                if buf_lines:
                    sections.append((buf_label, "\n".join(buf_lines)))
                buf_label = stripped
                buf_lines = []
            else:
                buf_lines.append(line)
        if buf_lines:
            sections.append((buf_label, "\n".join(buf_lines)))
        return sections

    @staticmethod
    def _build_chunks_from_sections(sections: list[tuple[str, str]],
                                     max_chars: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        for label, content in sections:
            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content, context_label=label))
            else:
                for sc in FallbackChunker().chunk(content, max_chars):
                    sc.context_label = label
                    sc.index = len(chunks)
                    chunks.append(sc)
        return chunks

    @staticmethod
    def _split_oversized_headed(text: str, label: str, max_chars: int) -> list[Chunk]:
        sub = FallbackChunker().chunk(text, max_chars)
        for sc in sub:
            sc.context_label = label
        return sub


# ═══════════════════════════════════════════════════════════
#  策略路由
# ═══════════════════════════════════════════════════════════

_CHUNKER_REGISTRY: dict[str, BaseChunker] = {
    "fallback": FallbackChunker(),
    "md": MarkdownChunker(),
    "py": CodeChunker(), "js": CodeChunker(), "ts": CodeChunker(),
    "java": CodeChunker(), "c": CodeChunker(), "cpp": CodeChunker(),
    "h": CodeChunker(), "go": CodeChunker(), "rs": CodeChunker(),
    "pdf": PDFChunker(),
    "xlsx": ExcelChunker(), "xls": ExcelChunker(),
    "docx": DocxChunker(),
}


def get_chunker(file_type: str) -> BaseChunker:
    key = file_type.lower() if file_type else ""
    return _CHUNKER_REGISTRY.get(key, _CHUNKER_REGISTRY["fallback"])


def chunk_text(file_type: str, text: str,
               max_chars: int | None = None) -> list[Chunk]:
    if max_chars is None:
        max_chars = settings.embedding_max_chars
    if not text or not text.strip():
        return [Chunk(index=0, text=text or "", context_label="")]

    chunker = get_chunker(file_type)
    try:
        chunks = chunker.chunk(text, max_chars)
        if not chunks:
            return [Chunk(index=0, text=text, context_label="")]
        return chunks
    except Exception:
        return [Chunk(index=0, text=text, context_label="")]
