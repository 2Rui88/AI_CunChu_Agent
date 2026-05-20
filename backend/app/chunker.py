"""
文件语义分块器 —— 按文件类型自适应切分长文本。

每种文件类型有独立的切分策略，利用文件原生的结构边界（段落、标题、
函数声明、页面等）作为切分点，embedding 模型 token 上限只做安全兜底。

架构：策略模式
  - BaseChunker    —— 抽象基类，定义统一接口
  - FallbackChunker —— 段落切分，适用所有文本类文件
  - PDFChunker     —— PDF 页面 + 字号突变检测（后续实现）
  - CodeChunker    —— 函数/类声明切分（后续实现）
  - MarkdownChunker —— ## 标题段落切分（后续实现）
  - ExcelChunker   —— 工作表 + 数据行切分（后续实现）
  - DocxChunker    —— 标题样式段落切分（后续实现）

使用入口：chunk_text(file_type, text, max_chars) -> list[Chunk]
"""

import re
from dataclasses import dataclass
from typing import Optional

# 默认分块字符数（模块级常量，避免导入 app.config 中的重量级依赖）
_DEFAULT_MAX_CHARS = 3000


@dataclass
class Chunk:
    """一个语义切片"""
    index: int          # 切片序号（0 起始）
    text: str           # 切片文本内容
    context_label: str  # 展示标注（"段落1-3" / "第3章" 等）


class BaseChunker:
    """分块器抽象基类，子类实现 chunk() 即可注册到策略路由"""

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        """
        将文本切分为多个语义切片。

        text:      提取后的原始文本
        max_chars: 单切片最大字符数（由 embedding 模型 token 上限决定）
        metadata:  补充信息（PDF 字体信息 / Excel sheet 名等）
        返回:      切片列表（按 index 排序），短文本返回单元素列表
        """
        raise NotImplementedError


class FallbackChunker(BaseChunker):
    """
    通用段落切分器 —— 以连续空行为边界切分。

    适用场景：纯文本、无结构的文件类型，以及所有未注册策略的类型。
    切分规则：
      1. 双换行符（空行）作为段落边界
      2. 合并相邻小段落至接近 max_chars
      3. 单个超大段落按句子边界二次切分（。！？）
      4. 每片取首句作为 context_label
    """

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        """按段落边界切分文本"""
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]

        # 如果文本本身不超过上限，不分块
        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        paragraphs = self._split_paragraphs(text)
        merged = self._merge_paragraphs(paragraphs, max_chars)
        chunks = self._build_chunks(merged, max_chars)
        return chunks

    # ── 内部方法 ──

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """将文本按空行拆分为段落列表，过滤纯空白段"""
        raw = re.split(r"\n{2,}", text)
        return [p.strip() for p in raw if p.strip()]

    @staticmethod
    def _merge_paragraphs(paragraphs: list[str], max_chars: int) -> list[str]:
        """
        合并相邻的小段落，使每段尽量接近 max_chars。
        单个段落超过 max_chars 不合并，留给 _split_oversized 处理。
        """
        merged: list[str] = []
        buf: list[str] = []

        for para in paragraphs:
            current_len = sum(len(p) for p in buf) + len(buf)  # len(buf) 为换行符数
            # 加上当前段落后不超过上限，合并
            if current_len + len(para) <= max_chars:
                buf.append(para)
            else:
                # 缓冲区有内容，先落盘
                if buf:
                    merged.append("\n\n".join(buf))
                buf = [para]

        if buf:
            merged.append("\n\n".join(buf))

        return merged

    @staticmethod
    def _split_oversized(block: str, max_chars: int) -> list[str]:
        """
        将超过 max_chars 的单个文本块按句子边界二次切分。
        句子边界：。！？后跟非空白字符（中文句子）或 .!? 后跟空格（英文句子）。
        """
        # 以句子分隔符为界切分
        parts = re.split(r"(?<=[。！？!?])(?=\S)", block)
        if len(parts) == 1:
            # 无句子分隔符，硬切
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
                # 单个句子超过上限，硬切
                if len(part) > max_chars:
                    for i in range(0, len(part), max_chars):
                        result.append(part[i:i + max_chars])
                    buf = []
                    buf_len = 0
                else:
                    buf = [part]
                    buf_len = len(part)

        if buf:
            result.append("".join(buf))

        return result

    @staticmethod
    def _build_chunks(merged_blocks: list[str], max_chars: int) -> list[Chunk]:
        """
        将合并后的文本块转为 Chunk 列表。
        超过限制的块走 _split_oversized，超限切片共享同一个 context_label。
        """
        chunks: list[Chunk] = []

        for block in merged_blocks:
            if len(block) <= max_chars:
                label = FallbackChunker._make_label(block)
                chunks.append(Chunk(index=len(chunks), text=block,
                                    context_label=label))
            else:
                sub_parts = FallbackChunker._split_oversized(block, max_chars)
                # 超限块的所有切片共享首句标签
                label = FallbackChunker._make_label(block)
                for part in sub_parts:
                    chunks.append(Chunk(index=len(chunks), text=part,
                                        context_label=label))

        return chunks

    @staticmethod
    def _make_label(text: str) -> str:
        """从文本首句生成 context_label（截取前 30 字）"""
        stripped = text.strip()
        if not stripped:
            return ""
        # 取第一个句子分隔符之前的内容
        match = re.search(r"[。！？.!?\n]", stripped)
        first = stripped[:match.start()] if match else stripped
        if len(first) > 30:
            first = first[:30] + "…"
        return first


class MarkdownChunker(BaseChunker):
    """
    Markdown 标题切分器 —— 以 ## 级标题为边界切分。

    适用场景：.md 文件，以及含 Markdown 标题的文档。
    切分规则：
      1. 以 ## 开头的行作为切分边界
      2. 标题行前的聚合文本归入上一节或作为无标题首节
      3. 单节超限时尝试 ### 子标题，再无则按段落切分
      4. context_label 取自所属标题行文本
    """

    _HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]

        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        sections = self._split_by_headings(text)
        chunks = self._build_chunks(sections, max_chars)
        return chunks

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str]]:
        """
        以 ## 标题为边界拆分文本。
        返回 [(label, content), ...]，首节 label 可为空。
        """
        # 找所有 ## 标题位置
        pattern = re.compile(r"^##\s+.+$", re.MULTILINE)
        matches = list(pattern.finditer(text))

        if not matches:
            return [("", text)]

        sections: list[tuple[str, str]] = []

        # 第一个标题之前的内容 → 无标题首节
        if matches[0].start() > 0:
            before = text[:matches[0].start()].strip()
            if before:
                sections.append(("", before))

        # 逐个标题区间
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
        """将标题-内容对转为 Chunk 列表，超限节递归用 ### 子标题切分"""
        chunks: list[Chunk] = []

        for label, content in sections:
            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content,
                                    context_label=label))
            else:
                # 超限节尝试 ### 子标题
                sub_sections = self._split_by_subheadings(content, label)
                for sub_label, sub_content in sub_sections:
                    if len(sub_content) <= max_chars:
                        chunks.append(Chunk(index=len(chunks),
                                            text=sub_content,
                                            context_label=sub_label or label))
                    else:
                        # 仍超限 → 回落段落切分
                        fallback = FallbackChunker()
                        sub_chunks = fallback.chunk(sub_content, max_chars)
                        for sc in sub_chunks:
                            sc.context_label = sub_label or label
                            sc.index = len(chunks)
                            chunks.append(sc)

        return chunks

    @staticmethod
    def _split_by_subheadings(content: str, parent_label: str
                              ) -> list[tuple[str, str]]:
        """
        以 ### 子标题拆分超限节内容。
        返回 [(label, content), ...]，无子标题则返回原始整节。
        """
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


class CodeChunker(BaseChunker):
    """
    代码函数/类切分器 —— 以函数声明和类声明为边界切分。

    适用场景：.py .js .ts .java .c .cpp .h .go .rs 等源代码文件。
    切分规则：
      1. 以函数声明（def / function / fn / func 等）或类声明（class）为边界
      2. 声明行之前的内容归入上一块或作为文件头（import / 注释等）
      3. 单函数超限不拆分（函数体完整性优先）
      4. context_label 取自函数名/类名
    """

    # 函数/类声明正则（匹配 Python/JS/TS/Java/C/C++/Go/Rust）
    _FUNC_CLASS_PATTERN = re.compile(
        r"^(\s*)"                        # 缩进（顶层为0）
        r"(?:"                           # 以下任一：
        r"(?:async\s+)?def\s+(\w+)"      #   Python async def / def
        r"|function\s+(\w+)"             #   JS function
        r"|(?:public\s+|private\s+|protected\s+|static\s+)*"  # 修饰符
        r"(?:class|interface)\s+(\w+)"   #   class / interface
        r"|fn\s+(\w+)"                   #   Rust fn
        r"|func\s+(\w+)"                 #   Go func
        r"|(?:void|int|char|float|double|long|short|bool|auto|"
        r"string|String|void)\s+(\w+)\s*\([^)]*\)\s*\{?"  # C/C++/Java 函数
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
            # 声明不足两个，整段作为一个切片
            label = self._first_func_name(matches) if matches else ""
            return self._oversized_chunks(text, label, max_chars)

        chunks: list[Chunk] = []

        # 第一个声明之前 → 文件头
        first = matches[0]
        if first.start() > 0:
            before = text[:first.start()].strip()
            if before:
                chunks.append(Chunk(index=0, text=before, context_label="文件头"))

        # 逐个函数/类区间
        for i, m in enumerate(matches):
            name = self._extract_name(m)
            label = self._make_func_label(m.group(), name)
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()

            if not content:
                continue

            if len(content) <= max_chars:
                chunks.append(Chunk(index=len(chunks), text=content,
                                    context_label=label))
            else:
                # 单函数超限不拆分，保证函数完整性
                sub = self._oversized_chunks(content, label, max_chars)
                for sc in sub:
                    sc.index = len(chunks)
                    chunks.append(sc)

        return chunks if chunks else self._oversized_chunks(text, "", max_chars)

    @staticmethod
    def _first_func_name(matches: list) -> str:
        """从首个匹配提取函数名"""
        return CodeChunker._extract_name(matches[0])

    @staticmethod
    def _extract_name(match: re.Match) -> str:
        """从正则匹配组中提取第一个有效名称（跳过缩进组和空字符串）"""
        for g in match.groups()[1:]:  # 跳过 group(1)（缩进）
            if g and g.strip():
                return g.strip()
        return ""

    @staticmethod
    def _make_func_label(raw_decl: str, name: str) -> str:
        """生成 context_label"""
        if name:
            return name
        return raw_decl.strip()[:40]

    @staticmethod
    def _oversized_chunks(text: str, label: str, max_chars: int) -> list[Chunk]:
        """超限时按 max_chars 硬切，所有切片共享同一 label"""
        chunks: list[Chunk] = []
        for i in range(0, len(text), max_chars):
            chunks.append(Chunk(index=len(chunks), text=text[i:i + max_chars],
                                context_label=label))
        return chunks


class PDFChunker(BaseChunker):
    """
    PDF 页面 + 字号检测切分器。

    适用场景：PDF 文件的逐页提取文本。
    切分规则：
      1. 以页面为基本单位（metadata["pages"] 传入每页文本列表）
      2. 相邻小页面合并至接近 max_chars
      3. 单页超大时按页面内的段落边界切分
      4. 字号突变信号预留入口（metadata["font_sizes"] 传入）
      5. context_label = "第N页" 或 "第N-M页"
    """

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        # 优先从 metadata 中获取逐页文本
        pages = (metadata or {}).get("pages", [])
        font_sizes = (metadata or {}).get("font_sizes", [])

        if pages:
            return self._chunk_by_pages(pages, font_sizes, max_chars)

        # 无页面信息 → 回落文本切分
        if not text or not text.strip():
            return [Chunk(index=0, text=text or "", context_label="")]

        if len(text) <= max_chars:
            return [Chunk(index=0, text=text, context_label="")]

        fallback = FallbackChunker()
        return fallback.chunk(text, max_chars)

    @staticmethod
    def _chunk_by_pages(pages: list[str], font_sizes: list,
                        max_chars: int) -> list[Chunk]:
        """按页面分组切分，合并相邻小页面"""
        chunks: list[Chunk] = []
        buf_pages: list[int] = []
        buf_text: list[str] = []
        buf_len = 0

        for i, page_text in enumerate(pages):
            text_i = page_text.strip()
            if not text_i:
                continue

            # 字号突变检测（预留扩展，当前仅记录）
            if i < len(font_sizes) and buf_pages:
                last_size = font_sizes[buf_pages[-1]] if buf_pages[-1] < len(font_sizes) else 0
                curr_size = font_sizes[i]
                if last_size and curr_size and abs(last_size - curr_size) > 4:
                    # 字号突变 → 落盘当前缓冲区
                    label = PDFChunker._page_range_label(buf_pages)
                    chunks.append(Chunk(index=len(chunks),
                                        text="\n".join(buf_text),
                                        context_label=label))
                    buf_pages, buf_text, buf_len = [], [], 0

            # 合并判断
            new_len = buf_len + len(text_i) + (1 if buf_text else 0)
            if new_len <= max_chars:
                buf_pages.append(i)
                buf_text.append(text_i)
                buf_len = new_len
            else:
                # 落盘缓冲区
                if buf_text:
                    label = PDFChunker._page_range_label(buf_pages)
                    chunks.append(Chunk(index=len(chunks),
                                        text="\n".join(buf_text),
                                        context_label=label))
                # 单页超大 → 单独成块
                if len(text_i) > max_chars:
                    label = PDFChunker._page_range_label([i])
                    sub_text = text_i[:max_chars]
                    chunks.append(Chunk(index=len(chunks), text=sub_text,
                                        context_label=label))
                    buf_pages, buf_text, buf_len = [], [], 0
                else:
                    buf_pages = [i]
                    buf_text = [text_i]
                    buf_len = len(text_i)

        if buf_text:
            label = PDFChunker._page_range_label(buf_pages)
            chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text),
                                context_label=label))

        return chunks

    @staticmethod
    def _page_range_label(pages: list[int]) -> str:
        """生成页面范围标签"""
        if len(pages) == 1:
            return f"第{pages[0] + 1}页"
        return f"第{pages[0] + 1}-{pages[-1] + 1}页"


class ExcelChunker(BaseChunker):
    """
    Excel 工作表切分器 —— 以工作表为一级边界，工作表内按行数分组。

    适用场景：.xlsx .xls 文件。
    切分规则：
      1. 每个工作表为一级边界
      2. 工作表内按 max_chars 对行分组
      3. context_label = "Sheet名" 或 "Sheet名 (行1-10)"
      4. 无 metadata 时回退 FallbackChunker
    """

    def chunk(self, text: str, max_chars: int,
              metadata: Optional[dict] = None) -> list[Chunk]:
        sheets = (metadata or {}).get("sheets", [])
        if not sheets:
            fallback = FallbackChunker()
            return fallback.chunk(text, max_chars)

        chunks: list[Chunk] = []
        for sheet in sheets:
            name = sheet.get("name", "")
            rows = sheet.get("rows", [])
            if not rows:
                continue
            sub = self._chunk_rows(rows, name, max_chars)
            for sc in sub:
                sc.index = len(chunks)
                chunks.append(sc)

        return chunks if chunks else [Chunk(index=0, text=text or "", context_label="")]

    @staticmethod
    def _chunk_rows(rows: list[list[str]], sheet_name: str,
                    max_chars: int) -> list[Chunk]:
        """将二维行列表逐行拼接为切片，以 max_chars 为上限分组"""
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
                    chunks.append(Chunk(index=len(chunks),
                                        text="\n".join(buf_text),
                                        context_label=label))
                # 单行超大 → 单独成块
                if len(row_str) > max_chars:
                    label = ExcelChunker._row_label(sheet_name, [i])
                    chunks.append(Chunk(index=len(chunks),
                                        text=row_str[:max_chars],
                                        context_label=label))
                    buf_rows, buf_text, buf_len = [], [], 0
                else:
                    buf_rows = [i]
                    buf_text = [row_str]
                    buf_len = len(row_str)

        if buf_text:
            label = ExcelChunker._row_label(sheet_name, buf_rows)
            chunks.append(Chunk(index=len(chunks), text="\n".join(buf_text),
                                context_label=label))

        return chunks

    @staticmethod
    def _row_label(sheet_name: str, row_indices: list[int]) -> str:
        """生成行范围标签"""
        if len(row_indices) == 1:
            return f"{sheet_name} (行{row_indices[0] + 1})"
        return f"{sheet_name} (行{row_indices[0] + 1}-{row_indices[-1] + 1})"


# ── 策略路由 ──

# 文件类型 → 分块器映射
_CHUNKER_REGISTRY: dict[str, BaseChunker] = {
    "fallback": FallbackChunker(),
    "md": MarkdownChunker(),
    "py": CodeChunker(),
    "js": CodeChunker(),
    "ts": CodeChunker(),
    "java": CodeChunker(),
    "c": CodeChunker(),
    "cpp": CodeChunker(),
    "h": CodeChunker(),
    "go": CodeChunker(),
    "rs": CodeChunker(),
    "pdf": PDFChunker(),
    "xlsx": ExcelChunker(),
    "xls": ExcelChunker(),
    # 后续阶段注册：
    # "docx": DocxChunker(),
}

# 可触发段落切分的文本类型（txt 及各类纯文本格式）
_TEXT_TYPES = {"txt", "md", "csv", "json", "xml",
               "html", "htm", "log", "yaml", "yml",
               "py", "js", "ts", "java", "c", "cpp", "h",
               "css", "sql", "sh", "bat", "ini", "cfg", "toml"}


def get_chunker(file_type: str) -> BaseChunker:
    """按文件类型返回对应的分块器实例，未注册类型走 FallbackChunker"""
    key = file_type.lower() if file_type else ""
    if key in _CHUNKER_REGISTRY:
        return _CHUNKER_REGISTRY[key]
    return _CHUNKER_REGISTRY["fallback"]


def chunk_text(file_type: str, text: str,
               max_chars: int | None = None) -> list[Chunk]:
    """
    对提取后的文件文本进行自适应分块。

    file_type: 文件扩展名（用于路由分块策略）
    text:      提取后的原始文本
    max_chars: 单切片最大字符数，默认取自配置
    返回:      切片列表，短文本返回单元素列表
    """
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
        # 分块异常时退回整段单切片
        return [Chunk(index=0, text=text, context_label="")]
