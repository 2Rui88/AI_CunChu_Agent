"""
Agent 对话模块 —— ReAct Agent Loop + SSE 流式输出

POST /agent/chat     —— 发起/继续对话 (SSE)
POST /agent/confirm  —— 确认/拒绝危险操作

工具调用走 DashScope OpenAI 兼容端点 (qwen-plus / qwen3.5-flash)
"""
import json
import uuid
import asyncio
import time
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from openai import AsyncOpenAI
from app.config import settings
from app.dependencies import check_token
from app.redis_client import redis
from app.agent_tools import TOOLS, DESTRUCTIVE_TOOLS

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ── 系统提示词 ──
SYSTEM_PROMPT = """你是 AI云存储 的文件管理助手。你可以帮助用户管理他们的文件。

你的能力:
- 语义搜索文件（search_files）
- 查看文件详细信息（get_file_info）
- 列出最近上传的文件（list_recent_files）
- 查看存储统计（get_storage_stats）
- 获取文件的 AI 内容描述（describe_file）
- 分享文件到共享广场（share_file）—— 需要用户确认
- 删除文件（delete_file）—— 需要用户确认

规则:
- 优先使用 search_files 查找文件
- 删除和分享前必须先向用户确认
- 回答要简洁，引用具体文件名和细节
- 如果搜索无结果，建议用户尝试不同描述词
- 始终使用中文回复"""

# ── 内存中的确认等待池 ──
_confirmation_pool: dict[str, asyncio.Event] = {}
_confirmation_results: dict[str, str] = {}


# ── 工具定义（OpenAI function calling 格式）──
def _tool_definitions() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "search_files",
            "description": "语义搜索用户文件",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "搜索关键词或自然语言描述"},
                "top_k": {"type": "integer", "description": "返回数量(默认5)", "default": 5},
            }, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "get_file_info",
            "description": "获取文件详细信息",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "文件MD5或文件名"},
            }, "required": ["identifier"]},
        }},
        {"type": "function", "function": {
            "name": "list_recent_files",
            "description": "列出最近上传的文件",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "description": "数量(默认10)", "default": 10},
            }},
        }},
        {"type": "function", "function": {
            "name": "get_storage_stats",
            "description": "查看存储统计",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "describe_file",
            "description": "获取文件的AI内容描述",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "文件MD5或文件名"},
            }, "required": ["identifier"]},
        }},
        {"type": "function", "function": {
            "name": "delete_file",
            "description": "删除文件（需用户确认）",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "文件MD5或文件名"},
            }, "required": ["identifier"]},
        }},
        {"type": "function", "function": {
            "name": "share_file",
            "description": "分享文件到共享广场（需用户确认）",
            "parameters": {"type": "object", "properties": {
                "identifier": {"type": "string", "description": "文件MD5或文件名"},
            }, "required": ["identifier"]},
        }},
    ]


class ChatRequest(BaseModel):
    user: str
    token: str
    api_key: str
    message: str
    conversation_id: str | None = None


class ConfirmRequest(BaseModel):
    user: str
    token: str
    confirmation_token: str
    decision: str


# ── 对话记忆（Redis）──

async def _load_messages(user: str, conv_id: str) -> list[dict]:
    """加载对话历史"""
    key = f"agent:conv:{user}:{conv_id}"
    raw = await redis.get(key)
    if raw:
        try:
            data = json.loads(raw)
            if "messages" in data:
                return data["messages"]
        except json.JSONDecodeError:
            pass
    return [{"role": "system", "content": SYSTEM_PROMPT}]


async def _save_messages(user: str, conv_id: str, messages: list[dict]):
    """保存对话历史（1小时过期）"""
    key = f"agent:conv:{user}:{conv_id}"
    data = json.dumps({
        "conv_id": conv_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages[-50:],
    }, ensure_ascii=False)
    await redis.setex(key, settings.conversation_ttl_seconds, data)


# ═══════════════════════════════════════════════════════════
#  SSE 对话接口
# ═══════════════════════════════════════════════════════════

@router.post("/chat")
async def agent_chat(body: ChatRequest):
    """Agent 对话入口 —— 返回 SSE 事件流"""
    if not await check_token(body.user, body.token):
        return EventSourceResponse(_error_stream("token error"))

    async def event_generator():
        # 加载或创建对话
        conv_id = body.conversation_id or str(uuid.uuid4())
        messages = await _load_messages(body.user, conv_id)
        if not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
        messages.append({"role": "user", "content": body.message})

        client = AsyncOpenAI(api_key=body.api_key, base_url=settings.dashscope_api_base)
        tool_count = 0

        try:
            for _ in range(settings.max_tool_calls_per_turn):
                yield await _sse("thinking", {"message": "思考中..."})

                # 调用 LLM
                try:
                    resp = await client.chat.completions.create(
                        model=settings.chat_model,
                        messages=messages,
                        tools=_tool_definitions(),
                        tool_choice="auto",
                    )
                except Exception as exc:
                    yield await _sse("error", {"message": f"LLM 调用失败: {exc}"})
                    break

                choice = resp.choices[0]
                msg = choice.message

                # 纯文本响应 → 结束
                if msg.content and not msg.tool_calls:
                    messages.append({"role": "assistant", "content": msg.content})
                    yield await _sse("message", {"delta": msg.content})
                    break

                # 工具调用
                if msg.tool_calls:
                    # 记录 assistant 消息（含 tool_calls）
                    messages.append({
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [
                            {"id": tc.id, "type": tc.type, "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }}
                            for tc in msg.tool_calls
                        ],
                    })

                    for tc in msg.tool_calls:
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            args = {}

                        yield await _sse("tool_call", {"name": name, "arguments": args})

                        # 危险操作确认门控
                        if name in DESTRUCTIVE_TOOLS:
                            confirm_token = str(uuid.uuid4())
                            event = asyncio.Event()
                            _confirmation_pool[confirm_token] = event

                            # 获取文件信息用于展示
                            file_info = {}
                            identifier = args.get("identifier", "")
                            try:
                                from app.agent_tools import tool_get_file_info
                                info = await tool_get_file_info(body.user, identifier=identifier)
                                if info.get("found"):
                                    file_info = {"filename": info["filename"], "size": str(info.get("size", "")),
                                                  "type": info.get("type", "")}
                            except Exception:
                                pass

                            msg_text = "确认删除" if name == "delete_file" else "确认分享"
                            yield await _sse("confirm_required", {
                                "confirmation_token": confirm_token,
                                "tool_name": name,
                                "tool_args": args,
                                "file_info": file_info,
                                "message": f"{msg_text} '{file_info.get('filename', identifier)}'？",
                            })

                            # 等待确认
                            try:
                                await asyncio.wait_for(event.wait(), timeout=300)
                            except asyncio.TimeoutError:
                                decision = "timeout"
                            else:
                                decision = _confirmation_results.pop(confirm_token, "rejected")
                            _confirmation_pool.pop(confirm_token, None)

                            if decision != "approved":
                                result = {"canceled": True, "message": "用户取消" if decision == "rejected" else "确认超时"}
                                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})
                                yield await _sse("tool_result", {"name": name, "result": result})
                                continue

                        # 执行工具
                        handler = TOOLS.get(name)
                        if handler:
                            try:
                                result = await handler(body.user, api_key=body.api_key, **args)
                                tool_count += 1
                            except Exception as exc:
                                result = {"error": f"工具执行失败: {exc}"}
                        else:
                            result = {"error": f"未知工具: {name}"}

                        yield await _sse("tool_result", {"name": name, "result": result})
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

            # 保存对话
            await _save_messages(body.user, conv_id, messages)
            yield await _sse("done", {"conv_id": conv_id, "total_tool_calls": tool_count})

        except Exception as exc:
            yield await _sse("error", {"message": str(exc)})

    return EventSourceResponse(event_generator())


# ═══════════════════════════════════════════════════════════
#  确认接口
# ═══════════════════════════════════════════════════════════

@router.post("/confirm")
async def agent_confirm(body: ConfirmRequest):
    """确认或拒绝危险操作"""
    if not await check_token(body.user, body.token):
        return {"code": 4}

    if body.confirmation_token not in _confirmation_pool:
        return {"code": 1, "msg": "confirmation token expired or invalid"}

    if body.decision not in ("approved", "rejected"):
        return {"code": 1, "msg": "decision must be approved or rejected"}

    _confirmation_results[body.confirmation_token] = body.decision
    _confirmation_pool[body.confirmation_token].set()
    return {"code": 0, "msg": body.decision}


async def _sse(event_type: str, data: dict) -> dict:
    """构造 SSE 事件字典"""
    return {"event": event_type, "data": json.dumps(data, ensure_ascii=False)}


async def _error_stream(msg: str):
    """返回错误 SSE 流"""
    yield {"event": "error", "data": json.dumps({"message": msg}, ensure_ascii=False)}
    yield {"event": "done", "data": json.dumps({"conv_id": "", "total_tool_calls": 0}, ensure_ascii=False)}
