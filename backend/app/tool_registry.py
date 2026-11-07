"""
Agent 工具注册表 —— 全部工具的唯一登记处（一张工具表）

工具的实现（业务函数）仍在 agent_tools.py；本模块用 ToolSpec 把每个工具的
LLM 侧元数据（描述 / 参数 Schema / 能力文案）与执行侧信息（handler / 是否
危险操作）收拢为一张显式登记表。路由层（routers/agent.py）只消费本表的
访问接口，不再各自维护平行的工具清单，也不直接导入任何工具实现函数：

    tool_definitions()    → OpenAI function calling 格式的工具定义列表
    get_tool(name)        → 按名取 ToolSpec（含 handler）
    is_destructive(name)  → 危险操作判定（确认门控用）
    abilities_section()   → 系统提示词"你的能力"列表（与表自动同步）

新增工具 = 在 agent_tools.py 写实现 + 在下面 TOOLS 表加一行 ToolSpec，
/chat 的工具定义与系统提示词能力清单随之自动同步，无需再改路由层。
"""
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.agent_tools import (
    tool_search_files, tool_get_file_info, tool_list_recent_files,
    tool_get_storage_stats, tool_describe_file, tool_read_file,
    tool_delete_file, tool_share_file,
)


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整登记项：LLM 侧元数据 + 执行侧 handler"""
    name: str                                   # 工具名（LLM function_call 按此调用）
    description: str                            # LLM 工具定义里的功能描述
    parameters: dict                            # 参数 JSON Schema（OpenAI 格式）
    handler: Callable[..., Awaitable[dict]]     # 实现：async def f(username, **kwargs) -> dict
    ability: str = ""                           # 系统提示词"你的能力"列表中的一行
    destructive: bool = False                   # 危险操作：执行前需用户二次确认


# ── 工具表（全站唯一登记处）──
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="search_files",
        description="语义搜索用户文件（全站唯一搜索入口，自然语言描述即可）",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词或自然语言描述"},
            "top_k": {"type": "integer", "description": "返回数量(默认5)", "default": 5},
        }, "required": ["query"]},
        handler=tool_search_files,
        ability="语义搜索文件（search_files，全站唯一搜索入口：查询规划 + 双路召回 + 统一排序）",
    ),
    ToolSpec(
        name="get_file_info",
        description="获取文件详细信息",
        parameters={"type": "object", "properties": {
            "identifier": {"type": "string", "description": "文件MD5或文件名"},
        }, "required": ["identifier"]},
        handler=tool_get_file_info,
        ability="查看文件详细信息（get_file_info）",
    ),
    ToolSpec(
        name="list_recent_files",
        description="列出最近上传的文件",
        parameters={"type": "object", "properties": {
            "count": {"type": "integer", "description": "数量(默认10)", "default": 10},
        }},
        handler=tool_list_recent_files,
        ability="列出最近上传的文件（list_recent_files）",
    ),
    ToolSpec(
        name="get_storage_stats",
        description="查看存储统计",
        parameters={"type": "object", "properties": {}},
        handler=tool_get_storage_stats,
        ability="查看存储统计（get_storage_stats）",
    ),
    ToolSpec(
        name="describe_file",
        description="获取文件的AI内容描述",
        parameters={"type": "object", "properties": {
            "identifier": {"type": "string", "description": "文件MD5或文件名"},
        }, "required": ["identifier"]},
        handler=tool_describe_file,
        ability="获取文件的 AI 内容描述（describe_file）",
    ),
    ToolSpec(
        name="read_file",
        description="分页读取文本文件的原始内容。返回 next_offset 非空时可用其继续读取后续内容，eof=true 表示已到文件末尾；非文本文件会被拒绝，请改用 describe_file",
        parameters={"type": "object", "properties": {
            "identifier": {"type": "string", "description": "文件MD5或文件名"},
            "offset": {"type": "integer", "description": "起始字节位置(默认0，从文件开头读)", "default": 0},
            "length": {"type": "integer", "description": "期望读取的字符数(默认4000,上限12000)", "default": 4000},
        }, "required": ["identifier"]},
        handler=tool_read_file,
        ability="分页读取文本文件的原始内容（read_file）",
    ),
    ToolSpec(
        name="delete_file",
        description="删除文件（需用户确认）",
        parameters={"type": "object", "properties": {
            "identifier": {"type": "string", "description": "文件MD5或文件名"},
        }, "required": ["identifier"]},
        handler=tool_delete_file,
        ability="删除文件（delete_file）—— 需要用户确认",
        destructive=True,
    ),
    ToolSpec(
        name="share_file",
        description="分享文件到共享广场（需用户确认）",
        parameters={"type": "object", "properties": {
            "identifier": {"type": "string", "description": "文件MD5或文件名"},
        }, "required": ["identifier"]},
        handler=tool_share_file,
        ability="分享文件到共享广场（share_file）—— 需要用户确认",
        destructive=True,
    ),
]

_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def all_tools() -> list[ToolSpec]:
    """全部已登记工具（登记顺序即 LLM 工具定义顺序）"""
    return list(TOOLS)


def get_tool(name: str) -> ToolSpec | None:
    """按名取工具登记项；未登记返回 None"""
    return _BY_NAME.get(name)


def is_destructive(name: str) -> bool:
    """是否危险操作（delete/share 需用户二次确认）"""
    spec = _BY_NAME.get(name)
    return bool(spec and spec.destructive)


def tool_definitions() -> list[dict]:
    """生成 OpenAI function calling 格式的工具定义列表（供 /chat 下发 LLM）"""
    return [
        {"type": "function", "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }}
        for spec in TOOLS
    ]


def abilities_section() -> str:
    """生成系统提示词"你的能力"列表（与工具表自动同步，不手工维护）"""
    return "\n".join(f"- {spec.ability}" for spec in TOOLS)
