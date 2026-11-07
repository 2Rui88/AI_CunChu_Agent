"""
工具注册表测试 —— 一张表驱动全部消费方，杜绝平行清单漂移

覆盖场景：
1.  表完整性：8 个工具、名字唯一、handler 全部为异步可调用
2.  tool_definitions：OpenAI function calling 结构合法、与表同名同序
3.  is_destructive：恰好 delete_file / share_file 为危险操作
4.  abilities_section：每工具一行、与表同步（含工具名括号）
5.  get_tool：命中 / 未登记名返回 None
6.  参数 Schema 合法性：type=object、required 里的字段都在 properties 中
7.  路由层解耦契约：源码不再直连 agent_tools 实现（读源码断言，
    不 import 路由模块，避免本地缺 sse_starlette 等容器内依赖）
"""
import inspect
from pathlib import Path

import pytest

from app.tool_registry import (
    TOOLS, abilities_section, all_tools, get_tool,
    is_destructive, tool_definitions,
)

AGENT_PY = Path(__file__).resolve().parent.parent / "app" / "routers" / "agent.py"

EXPECTED_NAMES = [
    "search_files", "get_file_info", "list_recent_files",
    "get_storage_stats", "describe_file", "read_file",
    "delete_file", "share_file",
]


class TestTableIntegrity:
    def test_eight_tools_in_registration_order(self):
        assert [s.name for s in TOOLS] == EXPECTED_NAMES

    def test_names_unique(self):
        names = [s.name for s in TOOLS]
        assert len(names) == len(set(names))

    def test_handlers_are_async(self):
        for spec in TOOLS:
            assert inspect.iscoroutinefunction(spec.handler), spec.name

    def test_all_fields_present(self):
        for spec in TOOLS:
            assert spec.name and spec.description
            assert isinstance(spec.parameters, dict)
            assert spec.ability, f"{spec.name} 缺能力文案（系统提示词列表会缺行）"

    def test_all_tools_returns_copy_in_order(self):
        assert [s.name for s in all_tools()] == EXPECTED_NAMES


class TestToolDefinitions:
    def test_openai_structure(self):
        defs = tool_definitions()
        assert len(defs) == len(TOOLS)
        for d, spec in zip(defs, TOOLS):
            assert d["type"] == "function"
            fn = d["function"]
            assert fn["name"] == spec.name
            assert fn["description"] == spec.description
            assert fn["parameters"] == spec.parameters

    def test_schema_validity(self):
        for spec in TOOLS:
            assert spec.parameters["type"] == "object"
            props = spec.parameters.get("properties", {})
            for req in spec.parameters.get("required", []):
                assert req in props, f"{spec.name}: required 字段 {req} 不在 properties"


class TestDestructive:
    def test_exactly_delete_and_share(self):
        destructive = {s.name for s in TOOLS if s.destructive}
        assert destructive == {"delete_file", "share_file"}

    def test_is_destructive(self):
        assert is_destructive("delete_file") is True
        assert is_destructive("share_file") is True
        for name in ("search_files", "read_file", "describe_file", "get_file_info",
                     "list_recent_files", "get_storage_stats"):
            assert is_destructive(name) is False

    def test_unknown_name_not_destructive(self):
        assert is_destructive("no_such_tool") is False


class TestAbilities:
    def test_one_line_per_tool(self):
        lines = abilities_section().splitlines()
        assert len(lines) == len(TOOLS)
        for line, spec in zip(lines, TOOLS):
            assert line == f"- {spec.ability}"
            assert spec.name in line  # 能力行必须点名工具，LLM 才能对上号


class TestGetTool:
    def test_hit(self):
        spec = get_tool("read_file")
        assert spec is not None
        assert spec.name == "read_file"
        assert inspect.iscoroutinefunction(spec.handler)

    def test_miss(self):
        assert get_tool("nope") is None


class TestNoParallelLists:
    def test_agent_router_not_importing_implementations(self):
        """路由层源码不得再直连 agent_tools 的实现或维护平行字典（读源码断言，
        避免 import 路由模块时拉起 sse_starlette 等容器内依赖）"""
        src = AGENT_PY.read_text(encoding="utf-8")
        assert "from app.agent_tools import" not in src
        assert "DESTRUCTIVE_TOOLS" not in src
        # 工具定义/查找/门控全部经注册表
        assert "tool_definitions()" in src
        assert "get_tool(" in src
        assert "is_destructive(" in src
