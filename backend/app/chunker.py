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


# ── 策略路由 ──

# 文件类型 → 分块器映射（逐步注册）
_CHUNKER_REGISTRY: dict[str, BaseChunker] = {
    "fallback": FallbackChunker(),
    "md": MarkdownChunker(),
    # 后续阶段注册：
    # "pdf": PDFChunker(),
    # "py": CodeChunker(),
    # "js": CodeChunker(),
    # "xlsx": ExcelChunker(),
    # "xls": ExcelChunker(),
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
