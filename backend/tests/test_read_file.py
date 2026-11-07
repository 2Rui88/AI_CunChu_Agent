"""
read_file 工具回归测试 —— 分页协议 / 参数钳制 / 类型门 / 行对齐与解码 / 截断标记

覆盖场景：
1.  _align_and_decode 行对齐：多字节字符 + 换行，消费字节数落在换行处
2.  _align_and_decode 无换行（超长单行）：按 max_chars*4 硬截
3.  _align_and_decode 硬截切中 UTF-8 多字节序列：退字节重试，不抛异常
4.  _align_and_decode 非 UTF-8（GBK 字节）：回落 latin-1
5.  _align_and_decode 空数据
6.  _clamp_read_params 正常值 / null / 负数 / 超界 / 非数字
7.  _is_readable_text 白名单扩展名 / 文件名特例 / 拒绝类型
8.  _format_text_description 截断标记：UTF-8 偏移 = 前缀编码字节数
9.  _format_text_description 截断标记：latin-1 偏移 = 字符预算（一字节一字符）
10. _format_text_description 未超预算：无标记
11. _format_text_description docx（allow_read_file=False）：标注截断但不给 offset
12. _decode_text 返回编码名（utf-8 / latin-1 / 双双失败）
13. _get_object_range 调用 MinIO 时传对 offset/length 并释放连接
14. tool_read_file 全链路（mock DB + MinIO）：信封字段、eof、续读衔接
15. tool_read_file 类型门拒绝、offset 越界、空文件
"""
import types

import pytest

from app import agent_tools
from app.agent_tools import (
    _align_and_decode, _clamp_read_params, _is_readable_text,
    tool_read_file,
)
from app.indexing.pipeline import _decode_text, _format_text_description


# ── 1-5 行对齐与解码 ──

class TestAlignAndDecode:
    def test_line_aligned_multibyte(self):
        # 3 行中文（UTF-8 一字 3 字节），行对齐应停在最后一个换行
        lines = ["第一行内容", "第二行内容", "第三行内容"]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        text, enc, consumed = _align_and_decode(data, 100)
        assert text == "\n".join(lines) + "\n"
        assert enc == "utf-8"
        assert consumed == len(data)

    def test_window_truncates_at_newline(self):
        # max_chars 限住窗口：窗口内最后一个换行处截断，后面半行留给下一页
        lines = ["aaaa\n", "bbbb\n", "cccc\n"]
        data = "".join(lines).encode()
        # 每行 5 字节；max_chars=1 → 窗口 4 字节，只含 "aaaa"，无换行→硬截?
        # 4 字节内没有 \n，走硬截分支返回 4 字节 "aaaa"
        text, enc, consumed = _align_and_decode(data, 1)
        assert text == "aaaa"
        assert consumed == 4
        # max_chars=2 → 窗口 8 字节："aaaa\nbbb"，最后换行在 4 → 消费 5 字节
        text, enc, consumed = _align_and_decode(data, 2)
        assert text == "aaaa\n"
        assert consumed == 5

    def test_no_newline_hard_truncation(self):
        data = b"a" * 100  # 无换行超长单行
        text, enc, consumed = _align_and_decode(data, 10)
        assert text == "a" * 40
        assert consumed == 40

    def test_at_file_end_includes_trailing_partial_line(self):
        # 缓冲已覆盖文件末尾：尾部残行（无换行）就是最后一行，直接消费，
        # 不对齐截断推迟 eof
        data = "第一段\n第二段\n最后一行".encode("utf-8")
        text, enc, consumed = _align_and_decode(data, 100, at_file_end=True)
        assert text == "第一段\n第二段\n最后一行"
        assert consumed == len(data)

    def test_at_file_end_respects_char_window(self):
        # 已到文件末尾但字符窗口更小：仍按窗口截断，余量留给下一页
        data = b"ab\n" * 10  # 30 字节
        text, enc, consumed = _align_and_decode(data, 2, at_file_end=True)  # 窗口 8 字节
        assert consumed == 8
        assert text == "ab\nab\nab"

    def test_partial_multibyte_at_hard_truncation(self):
        # 硬截点恰好切在 3 字节中文的中间：应退字节到完整字符，不抛异常
        data = ("汉" * 20).encode("utf-8")  # 60 字节，无换行
        text, enc, consumed = _align_and_decode(data, 5)  # 窗口 20 字节
        # 20 = 6 汉(18B) + 2B 残缺 → 退 2 字节，返回 6 个"汉"，消费 18
        assert text == "汉" * 6
        assert enc == "utf-8"
        assert consumed == 18

    def test_latin1_fallback(self):
        # GBK 编码的中文按 UTF-8 解必失败 → latin-1 兜底，一字节一字符
        data = "中文内容".encode("gbk") + b"\n"
        text, enc, consumed = _align_and_decode(data, 100)
        assert enc == "latin-1"
        assert consumed == len(data)
        assert len(text) == len(data)  # latin-1 字符数 == 字节数

    def test_empty_data(self):
        text, enc, consumed = _align_and_decode(b"", 100)
        assert text == ""
        assert consumed == 0


# ── 6 参数钳制 ──

class TestClampParams:
    def test_defaults(self):
        assert _clamp_read_params(None, None) == (0, 4000)

    def test_normal_values(self):
        assert _clamp_read_params(123, 2000) == (123, 2000)

    def test_null_and_garbage(self):
        # LLM 传 null / 字符串数字 / 非数字：回落默认，不抛错
        assert _clamp_read_params(None, None) == (0, 4000)
        assert _clamp_read_params("100", "8000") == (100, 8000)
        assert _clamp_read_params("abc", "xyz") == (0, 4000)

    def test_negative_offset(self):
        assert _clamp_read_params(-5, 1000) == (0, 1000)

    def test_length_clamped_both_ways(self):
        assert _clamp_read_params(0, 1)[1] == 500       # 下限
        assert _clamp_read_params(0, 99999)[1] == 12000  # 上限
        assert _clamp_read_params(0, -3)[1] == 500       # 负数走下限


# ── 7 类型门 ──

class TestIsReadableText:
    @pytest.mark.parametrize("name", [
        "a.txt", "notes.md", "app.log", "data.csv", "c.json", "x.yaml",
        "main.py", "index.js", "App.ts", "Comp.vue", "main.go", "lib.rs",
        "query.sql", "run.sh", "style.css", "page.html", "conf.ini",
    ])
    def test_whitelist(self, name):
        assert _is_readable_text(name) is True

    @pytest.mark.parametrize("name", ["Dockerfile", "Makefile", "README", "LICENSE"])
    def test_special_names(self, name):
        assert _is_readable_text(name) is True

    def test_dotfiles_by_suffix(self):
        # _get_suffix(".gitignore") 取到 "gitignore"，走类型白名单而非文件名特例
        assert _is_readable_text(".gitignore") is True

    @pytest.mark.parametrize("name", [
        "photo.png", "video.mp4", "doc.pdf", "sheet.xlsx", "paper.docx",
        "archive.zip", "binary.exe", "song.mp3", "unknown",
    ])
    def test_rejected(self, name):
        assert _is_readable_text(name) is False


# ── 8-11 截断标记 ──

class TestTruncationMarker:
    def test_utf8_marker_byte_offset(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "extract_char_budget", 10, raising=False)
        content = "汉" * 25  # 25 字符，超预算 10
        desc = _format_text_description("f.txt", content, "utf-8")
        # 截断前缀 10 个汉字 = 30 字节，offset 必须是 30 而非 10
        assert "offset=30" in desc
        assert "共 25 字符" in desc
        assert "read_file" in desc
        assert desc.count("汉") == 10

    def test_latin1_marker_byte_offset(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "extract_char_budget", 10, raising=False)
        content = "Ä" * 25  # latin-1 一字符一字节
        desc = _format_text_description("f.log", content, "latin-1")
        assert "offset=10" in desc

    def test_no_marker_under_budget(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "extract_char_budget", 100, raising=False)
        desc = _format_text_description("f.txt", "短内容", "utf-8")
        assert "offset" not in desc
        assert "read_file" not in desc

    def test_docx_marker_without_offset(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "extract_char_budget", 10, raising=False)
        desc = _format_text_description("f.docx", "字" * 25, "utf-8", allow_read_file=False)
        assert "offset" not in desc
        assert "read_file" not in desc
        assert "共 25 字符" in desc  # 仍标注截断事实


# ── 12 _decode_text 编码名 ──

class TestDecodeText:
    def test_utf8(self):
        assert _decode_text("中文".encode("utf-8")) == ("中文", "utf-8")

    def test_latin1(self):
        raw = "中文".encode("gbk")
        text, enc = _decode_text(raw)
        assert enc == "latin-1"
        assert text == raw.decode("latin-1")

    def test_empty(self):
        assert _decode_text(b"") == ("", "utf-8")


# ── 13 MinIO 范围读 ──

class TestGetObjectRange:
    def test_passes_offset_length_and_releases(self, monkeypatch):
        calls = {}

        class FakeResp:
            def read(self):
                return b"payload"
            def close(self):
                calls["closed"] = True
            def release_conn(self):
                calls["released"] = True

        class FakeMinio:
            def get_object(self, bucket, obj, offset=None, length=None):
                calls.update(bucket=bucket, obj=obj, offset=offset, length=length)
                return FakeResp()

        monkeypatch.setattr(agent_tools, "minio_client", FakeMinio())
        data = agent_tools._get_object_range("md5/name", 100, 50)
        assert data == b"payload"
        assert calls["bucket"] == agent_tools.BUCKET
        assert calls["obj"] == "md5/name"
        assert calls["offset"] == 100
        assert calls["length"] == 50
        assert calls["closed"] and calls["released"]


# ── 14-15 tool_read_file 全链路（mock Session + MinIO）──

def _make_row(filename, file_id, size, md5="m d5".replace(" ", "")):
    """伪 UserFileList + FileInfo 行"""
    ufl = types.SimpleNamespace(md5=md5, file_name=filename)
    fi = types.SimpleNamespace(md5=md5, file_id=file_id, size=size, type=filename.rsplit(".", 1)[-1])
    return (ufl, fi)


class _FakeResult:
    def __init__(self, row):
        self._row = row
    def first(self):
        return self._row


class _FakeSession:
    """按 agent_tools.Session 的用法伪造：async with Session() as db → db.execute().first()"""
    row = None

    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kw):
        return _FakeResult(_FakeSession.row)


class TestToolReadFile:
    def _patch(self, monkeypatch, row, obj_bytes):
        _FakeSession.row = row
        monkeypatch.setattr(agent_tools, "Session", _FakeSession)

        class FakeResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def close(self):
                pass
            def release_conn(self):
                pass

        class FakeMinio:
            def get_object(self, bucket, obj, offset=None, length=None):
                # 模拟范围读：按 offset/length 切全量字节
                return FakeResp(obj_bytes[offset:offset + length])

        monkeypatch.setattr(agent_tools, "minio_client", FakeMinio())

    @pytest.mark.asyncio
    async def test_envelope_and_eof(self, monkeypatch):
        text = "第一段落\n第二段落\n最后一行"  # 无结尾换行
        raw = text.encode("utf-8")
        self._patch(monkeypatch, _make_row("note.md", "obj1", len(raw), "md5a"), raw)
        result = await tool_read_file("u", identifier="note.md")
        assert result["success"] is True
        assert result["md5"] == "md5a"
        assert result["size"] == len(raw)
        assert result["offset"] == 0
        assert result["content"] == text
        assert result["chars"] == len(text)
        assert result["eof"] is True
        assert result["next_offset"] is None
        assert result["encoding"] == "utf-8"

    @pytest.mark.asyncio
    async def test_pagination_chain(self, monkeypatch):
        # 3 行、每行 5 字节；页大小钳到 500 → 窗口 2000 字节全量，行对齐吃到末换行
        raw = b"line\nline\nline\n"
        self._patch(monkeypatch, _make_row("a.log", "obj2", len(raw), "md5b"), raw)
        r1 = await tool_read_file("u", identifier="a.log", offset=0)
        assert r1["content"] == "line\nline\nline\n"
        assert r1["eof"] is True
        assert r1["next_offset"] is None

        # 从中间读：offset=5 → "line\nline\n"，eof=True（读到末尾）
        r2 = await tool_read_file("u", identifier="a.log", offset=5)
        assert r2["content"] == "line\nline\n"
        assert r2["eof"] is True

    @pytest.mark.asyncio
    async def test_next_offset_boundary(self, monkeypatch):
        from app.config import settings
        # 缩小钳制值做字节级精确断言：min=1/max=4/slack=0 → length=2 时窗口 8 字节
        monkeypatch.setattr(settings, "read_file_min_chars", 1, raising=False)
        monkeypatch.setattr(settings, "read_file_max_chars", 4, raising=False)
        monkeypatch.setattr(settings, "read_file_slack_bytes", 0, raising=False)
        raw = b"line\nline\nline\nline"  # 20 字节，末行无换行
        self._patch(monkeypatch, _make_row("b.log", "obj3", len(raw), "md5c"), raw)

        pages, offset = [], 0
        for _ in range(10):
            r = await tool_read_file("u", identifier="b.log", offset=offset, length=2)
            assert r["success"] is True
            pages.append(r["content"])
            if r["eof"]:
                assert r["next_offset"] is None
                break
            offset = r["next_offset"]
        # 翻页无缝衔接：无重复、无遗漏、末行残行在最后一页收尾
        assert pages == ["line\n", "line\n", "line\n", "line"]
        assert "".join(pages).encode() == raw

    @pytest.mark.asyncio
    async def test_unsupported_type(self, monkeypatch):
        self._patch(monkeypatch, _make_row("movie.mp4", "obj4", 1000, "md5d"), b"x" * 100)
        result = await tool_read_file("u", identifier="movie.mp4")
        assert result["success"] is False
        assert result["reason"] == "unsupported_type"
        assert "describe_file" in result["message"]

    @pytest.mark.asyncio
    async def test_offset_beyond_eof(self, monkeypatch):
        raw = b"hello\n"
        self._patch(monkeypatch, _make_row("c.txt", "obj5", len(raw), "md5e"), raw)
        result = await tool_read_file("u", identifier="c.txt", offset=9999)
        assert result["success"] is False
        assert "offset" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_file(self, monkeypatch):
        self._patch(monkeypatch, _make_row("empty.txt", "obj6", 0, "md5f"), b"")
        result = await tool_read_file("u", identifier="empty.txt")
        assert result["success"] is True
        assert result["eof"] is True
        assert result["content"] == ""

    @pytest.mark.asyncio
    async def test_missing_identifier(self):
        result = await tool_read_file("u")
        assert result == {"error": "missing identifier"}

    @pytest.mark.asyncio
    async def test_not_found(self, monkeypatch):
        self._patch(monkeypatch, None, b"")
        result = await tool_read_file("u", identifier="ghost.txt")
        assert result["success"] is False
        assert "不存在" in result["message"]

    @pytest.mark.asyncio
    async def test_llm_garbage_params(self, monkeypatch):
        raw = "内容行\n".encode("utf-8") * 3
        self._patch(monkeypatch, _make_row("d.log", "obj7", len(raw), "md5g"), raw)
        # null / 负数 / 超界：全部就地修正不抛错
        r = await tool_read_file("u", identifier="d.log", offset=None, length=None)
        assert r["success"] is True and r["offset"] == 0
        r = await tool_read_file("u", identifier="d.log", offset=-5, length=-1)
        assert r["success"] is True and r["offset"] == 0
