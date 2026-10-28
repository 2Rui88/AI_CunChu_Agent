"""
Agent 对话模块 —— ReAct Agent Loop + SSE 流式输出

POST /agent/chat     —— 发起/继续对话 (SSE)
POST /agent/confirm  —— 确认/拒绝危险操作

工具调用走 DashScope OpenAI 兼容端点 (qwen-plus / qwen3.5-flash)
"""
import json
import re
import uuid
import asyncio
import logging
from urllib.parse import unquote
from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from openai import AsyncOpenAI
from sqlalchemy import select, func
from app.config import settings
from app.database import Session
from app.models import AgentConversation, AgentMessage
from app.dependencies import check_token
from app.redis_client import redis
from app.agent_tools import TOOLS, DESTRUCTIVE_TOOLS
from app.minio_client import presign_url, BUCKET

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ── 系统提示词 ──
SYSTEM_PROMPT = """你是 AI云存储 的文件管理助手，也是用户查找文件的唯一入口。你可以帮助用户搜索和管理他们的文件。

你的能力:
- 语义搜索文件（search_files，全链路：查询改写 + 双路召回 + 重排序）
- 查看文件详细信息（get_file_info）
- 列出最近上传的文件（list_recent_files）
- 查看存储统计（get_storage_stats）
- 获取文件的 AI 内容描述（describe_file）
- 分享文件到共享广场（share_file）—— 需要用户确认
- 删除文件（delete_file）—— 需要用户确认

规则:
- 用户想找文件时，优先使用 search_files，不需要引导他去别处
- 需要定位具体文件时，可用 list_recent_files，或按用户提供的文件名/MD5 调用 get_file_info
- 删除和分享前必须先向用户确认
- 回答要简洁，引用具体文件名和细节
- 引用文件时，用 Markdown 链接格式 [文件名](URL) 附带下载链接，URL 必须原样使用工具结果中给出的链接，不得改写、缩写或省略问号后的任何参数
- 如果搜索结果有 match_context，用它标注文件中的具体位置
- 如果搜索无结果，建议用户尝试不同的描述词
- 始终使用中文回复"""

# ── 危险操作确认（Redis 实现，跨 gunicorn worker 共享）──
# 旧实现为进程内存字典，/confirm 落到其他 worker 时永远找不到确认单，
# 只能等超时。迁到 Redis 后任意 worker 都能读写同一条确认记录。
CONFIRM_KEY_PREFIX = "agent:confirm:"
CONFIRM_TTL = 310              # 略长于等待超时，未决确认自动过期清理
CONFIRM_POLL_INTERVAL = 0.5    # 轮询间隔（秒）
CONFIRM_WAIT_TIMEOUT = 300     # 等待用户确认的超时（秒）


# ── 答案内 /dl/ 下载链接重签 ──
# 私有桶下载必须带 SigV4 签名，而 LLM 复述工具返回的链接时常丢掉问号后的
# 签名参数（裸 /dl/ 链接点击 403）。答案逐 token 流式下发期间无法拦截链接碎片，
# 收尾时对完整全文重签一次，经 message_replace 事件整体替换前端已显示的原文；
# 落库同样存修正版，历史会话里的链接也可点。
_DL_LINK_RE = re.compile(r"\((\s*)((?:https?://[^)\s]*)?/dl/[^)\s]*)(\s*)\)")


def _resign_dl_links(text: str) -> str:
    """把答案中指向 /dl/ 的下载链接重签为带有效签名的完整 URL。"""
    def _repl(m: re.Match) -> str:
        url = m.group(2)
        if "X-Amz-Signature" in url:
            return m.group(0)  # 已带签名，原样保留
        path = url.split("?", 1)[0]
        marker = f"/dl/{BUCKET}/"
        pos = path.find(marker)
        if pos < 0:
            return m.group(0)
        object_name = unquote(path[pos + len(marker):])
        if not object_name:
            return m.group(0)
        try:
            return f"({m.group(1)}{presign_url(object_name)}{m.group(3)})"
        except Exception:
            return m.group(0)  # 重签失败退回原文，不阻断回答
    return _DL_LINK_RE.sub(_repl, text)


async def _create_confirmation(token: str, user: str, tool_name: str):
    """登记一条待确认记录（所有 worker 经 Redis 可见）"""
    payload = json.dumps(
        {"user": user, "tool": tool_name, "decision": ""},
        ensure_ascii=False,
    )
    await redis.setex(f"{CONFIRM_KEY_PREFIX}{token}", CONFIRM_TTL, payload)


async def _wait_confirmation(token: str, timeout: int) -> str:
    """轮询等待用户决策，返回 approved / rejected / timeout"""
    key = f"{CONFIRM_KEY_PREFIX}{token}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        raw = await redis.get(key)
        if raw:
            try:
                decision = json.loads(raw).get("decision", "")
            except json.JSONDecodeError:
                decision = ""
            if decision:
                return decision
        await asyncio.sleep(CONFIRM_POLL_INTERVAL)
    return "timeout"


# ── 工具定义（OpenAI function calling 格式）──
def _tool_definitions() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "search_files",
            "description": "语义搜索用户文件（全站唯一搜索入口，自然语言描述即可）",
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
    message: str
    conversation_id: str | None = None
    # 注意：不再接收客户端 api_key —— 统一使用服务端 DASHSCOPE_API_KEY 配置


class ConfirmRequest(BaseModel):
    user: str
    token: str
    confirmation_token: str
    decision: str


class ConversationListRequest(BaseModel):
    user: str
    token: str
    start: int = 0          # 分页参数与 /myfiles 等列表接口的 start/count 对齐
    count: int = 30


class ConversationMessagesRequest(BaseModel):
    user: str
    token: str
    conv_id: str
    before_seq: int | None = None   # 传入则取该 seq 之前的一页（向上翻更早消息）
    count: int = 50


class ConversationDeleteRequest(BaseModel):
    user: str
    token: str
    conv_id: str


# ── 会话持久化（MySQL 为唯一真相源）──
# 旧实现整段对话存 Redis（1 小时过期、仅保留最近 20 条），刷新页面或超时即丢。
# 现在会话元数据与全量消息落 MySQL；Redis 仅保留危险操作确认单。
# system 提示词不落库，组装 LLM 上下文时动态注入，改提示词不影响存量数据。

DEFAULT_TITLE = "新对话"
TITLE_MAX_LEN = 20


async def _get_active_conversation(user: str, conv_key: str | None) -> AgentConversation:
    """按 conv_key 查找用户的有效会话，查不到则新建。

    conv_key 全局唯一且软删除行仍占用该键，因此"已删除/已过期/不归属本用户"
    一视同仁：生成全新 conv_key 开新会话，经 done 事件回传前端切换。
    """
    async with Session() as db:
        if conv_key:
            row = (await db.execute(
                select(AgentConversation).where(
                    AgentConversation.conv_key == conv_key,
                    AgentConversation.user == user,
                    AgentConversation.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if row:
                return row
        row = AgentConversation(conv_key=str(uuid.uuid4()), user=user)
        db.add(row)
        await db.commit()
        return row


async def _persist_messages(conv_id: int, msgs: list[dict], *, set_title: str = "") -> None:
    """事务内行锁分配 seq 写入一批消息，并维护会话统计。

    FOR UPDATE 锁会话行串行化 seq 分配，并发请求不会产生重号/错序；
    set_title 仅在标题仍为默认值时生效（取首条用户消息摘要）。
    """
    if not msgs and not set_title:
        return
    async with Session() as db:
        async with db.begin():
            conv = (await db.execute(
                select(AgentConversation)
                .where(AgentConversation.id == conv_id)
                .with_for_update()
            )).scalar_one()
            seq = conv.last_seq
            for m in msgs:
                seq += 1
                db.add(AgentMessage(
                    conv_id=conv_id,
                    seq=seq,
                    role=m["role"],
                    content=m.get("content"),
                    tool_calls=m.get("tool_calls"),
                    tool_call_id=m.get("tool_call_id"),
                ))
            conv.last_seq = seq
            conv.message_count += len(msgs)
            conv.update_time = func.now()
            if set_title and conv.title == DEFAULT_TITLE:
                conv.title = set_title


def _row_to_llm(row: AgentMessage) -> dict:
    """消息行 → OpenAI 消息结构"""
    m: dict = {"role": row.role, "content": row.content or ""}
    if row.tool_calls:
        m["tool_calls"] = row.tool_calls
    if row.tool_call_id:
        m["tool_call_id"] = row.tool_call_id
    return m


async def _load_stored_messages(conv_id: int, limit: int = 80) -> list[dict]:
    """读取会话最近 limit 条消息（倒序限量后反转为正序），供组装 LLM 上下文。

    limit 取上下文窗口的 4 倍，覆盖截断后向 user 边界回扩的余量。
    """
    async with Session() as db:
        rows = (await db.execute(
            select(AgentMessage)
            .where(AgentMessage.conv_id == conv_id)
            .order_by(AgentMessage.seq.desc())
            .limit(limit)
        )).scalars().all()
    return [_row_to_llm(r) for r in reversed(rows)]


def _safe_prefix(msgs: list[dict]) -> list[dict]:
    """截掉收尾不完整的一轮：assistant 的每个 tool_calls 都必须有对应 tool 回应。

    这批消息入库后会被下一轮对话组装进 LLM 上下文，带上悬空 tool_calls
    （有调用无结果）会被 DashScope 直接 400 拒绝，所以兜底存档只存安全前缀。
    """
    pending: set[str] = set()
    safe = 0
    for i, m in enumerate(msgs):
        for tc in m.get("tool_calls") or []:
            if tc.get("id"):
                pending.add(tc["id"])
        if m.get("role") == "tool" and m.get("tool_call_id"):
            pending.discard(m["tool_call_id"])
        if not pending:
            safe = i + 1
    return msgs[:safe]


# ── 断线/中断兜底存档的后台任务 ──
# 生成器被取消后 finally 中不能再 await；把存档作为独立任务挂在事件循环上跑完。
# 集合持有任务引用，防止任务对象被 GC 中途回收（完成后自动移出）。
_bg_persist_tasks: set[asyncio.Task] = set()


def _spawn_persist(conv_id: int, msgs: list[dict]) -> None:
    async def _run() -> None:
        try:
            await _persist_messages(conv_id, msgs)
        except Exception:
            pass  # 兜底重试也失败则容忍丢失：提问已先落库，用户重问即可

    task = asyncio.create_task(_run())
    _bg_persist_tasks.add(task)
    task.add_done_callback(_bg_persist_tasks.discard)


def _build_llm_context(history: list[dict], new_user_message: dict) -> list[dict]:
    """system + 截断历史 + 本条用户消息。

    截断保留最近 max_context_messages 条，且起点必须落在 user 消息上：
    OpenAI 接口要求 tool 消息紧跟其 assistant tool_calls，整轮保留才不会
    切断调用对（宁可比窗口多带几条，不向内切入半轮）。
    """
    window = history + [new_user_message]
    cut = max(0, len(window) - settings.max_context_messages)
    while cut > 0 and window[cut]["role"] != "user":
        cut -= 1
    return [{"role": "system", "content": SYSTEM_PROMPT}] + window[cut:]


# ═══════════════════════════════════════════════════════════
#  SSE 对话接口
# ═══════════════════════════════════════════════════════════

@router.post("/chat")
async def agent_chat(body: ChatRequest):
    """Agent 对话入口 —— 返回 SSE 事件流"""
    if not await check_token(body.user, body.token):
        return EventSourceResponse(_error_stream("token error"))

    if not settings.dashscope_api_key:
        return EventSourceResponse(_error_stream("AI 服务未配置，请联系管理员"))

    async def event_generator():
        # 解析/创建会话并组装上下文（历史来自 MySQL，system 动态注入）
        conv = await _get_active_conversation(body.user, body.conversation_id)
        user_msg = {"role": "user", "content": body.message}
        messages = _build_llm_context(await _load_stored_messages(conv.id), user_msg)

        # 用户消息先落库：LLM 失败或连接中断也不丢提问，会话列表立即可见
        title = " ".join(body.message.split())[:TITLE_MAX_LEN].strip()
        try:
            await _persist_messages(conv.id, [user_msg], set_title=title or DEFAULT_TITLE)
        except Exception:
            yield await _sse("error", {"message": "会话保存失败，请稍后重试"})
            yield await _sse("done", {"conv_id": "", "total_tool_calls": 0})
            return

        client = AsyncOpenAI(api_key=settings.dashscope_api_key, base_url=settings.dashscope_api_base)
        tool_count = 0
        base = len(messages)        # 本轮 assistant/tool 消息的追加起点
        committed = base            # 已落库的消息下标（分步提交的安全边界）
        streamed_text = ""          # 本轮已流式下发、尚未定稿进 messages 的文本

        async def commit(upto: int) -> None:
            """把 messages[committed:upto] 落库并推进边界；失败保留边界由 finally 兜底重试。"""
            nonlocal committed
            if upto <= committed:
                return
            try:
                await _persist_messages(conv.id, messages[committed:upto])
                committed = upto
            except Exception:
                pass

        try:
            for _ in range(settings.max_tool_calls_per_turn):
                yield await _sse("thinking", {"message": "思考中..."})

                # 流式调用 LLM：文本增量即刻下发；工具调用分片按 index 拼装
                text = ""                    # 本轮累计文本（落库/重签用）
                calls: dict[int, dict] = {}  # index -> {id, name, arguments}
                try:
                    stream = await client.chat.completions.create(
                        model=settings.chat_model,
                        messages=messages,
                        tools=_tool_definitions(),
                        tool_choice="auto",
                        stream=True,
                    )
                    async with stream:  # 断开/异常时确定性关闭上游连接
                        async for chunk in stream:
                            if not chunk.choices:
                                continue  # 流末尾的 usage 等空 choices 块
                            d = chunk.choices[0].delta
                            if d is None:
                                continue
                            if d.content:
                                text += d.content
                                streamed_text += d.content
                                yield await _sse("message", {"delta": d.content})
                            if d.tool_calls:
                                for frag in d.tool_calls:
                                    acc = calls.setdefault(frag.index or 0, {"id": "", "name": "", "arguments": ""})
                                    if frag.id:
                                        acc["id"] = frag.id
                                    if frag.function and frag.function.name:
                                        acc["name"] += frag.function.name
                                    if frag.function and frag.function.arguments:
                                        acc["arguments"] += frag.function.arguments
                except Exception as exc:
                    logging.getLogger("uvicorn.error").exception("agent/chat LLM 流式调用失败")
                    yield await _sse("error", {"message": f"LLM 调用失败: {exc}"})
                    break

                tool_calls = [
                    {"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": c["arguments"],
                    }}
                    for _, c in sorted(calls.items())
                ]
                # name 嵌在 function 里（OpenAI 消息格式），别按拼装前的扁平结构取
                if any(not c["id"] or not c["function"]["name"] for c in tool_calls):
                    yield await _sse("error", {"message": "LLM 返回的工具调用不完整，请重试"})
                    break

                # 纯文本响应 → 最终答案：全文重签下载链接（防 LLM 丢签名参数），
                # 有修正时经 message_replace 整体替换前端已流式显示的原文
                if not tool_calls:
                    if not text:
                        continue  # 模型空响应：与旧实现一致，进入下一轮重试
                    final = _resign_dl_links(text)
                    if final != text:
                        yield await _sse("message_replace", {"content": final})
                    messages.append({"role": "assistant", "content": final})
                    streamed_text = ""  # 已定稿进 messages，兜底存档不再单独补
                    await commit(len(messages))
                    break

                # 工具调用轮：本轮 text 作为前言与 tool_calls 一并记录
                messages.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": tool_calls,
                })

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    yield await _sse("tool_call", {"name": name, "arguments": args})

                    # 危险操作确认门控
                    if name in DESTRUCTIVE_TOOLS:
                        confirm_token = str(uuid.uuid4())
                        await _create_confirmation(confirm_token, body.user, name)

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

                        # 等待确认（Redis 轮询，任意 worker 的 /confirm 都能送达）
                        decision = await _wait_confirmation(confirm_token, CONFIRM_WAIT_TIMEOUT)

                        if decision != "approved":
                            result = {"canceled": True, "message": "用户取消" if decision == "rejected" else "确认超时"}
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
                            yield await _sse("tool_result", {"name": name, "result": result})
                            continue

                    # 执行工具（密钥由服务端配置提供，不经过 LLM 参数）
                    handler = TOOLS.get(name)
                    if handler:
                        try:
                            result = await handler(body.user, **args)
                            tool_count += 1
                        except Exception as exc:
                            result = {"error": f"工具执行失败: {exc}"}
                    else:
                        result = {"error": f"未知工具: {name}"}

                    yield await _sse("tool_result", {"name": name, "result": result})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})

                # 整轮完成原子落库：assistant 的 tool_calls 必须与全部 tool 回应同批，
                # 只存半批会让下一轮组装 LLM 上下文时带上悬空 tool_calls 被 API 拒绝
                await commit(len(messages))
                streamed_text = ""

            # done 之前本轮内容均已分步落库；若最后一步 commit 失败，
            # finally 的兜底任务会补上（提问已先落库，重开页面仍可见）
            yield await _sse("done", {"conv_id": conv.conv_key, "total_tool_calls": tool_count})

        except Exception as exc:
            # 逐 token 流式改造后错误面变宽，必须留 traceback 才能定位（前端只见 str(exc)）
            logging.getLogger("uvicorn.error").exception("agent/chat 未捕获异常")
            yield await _sse("error", {"message": str(exc)})
        finally:
            # 中断/异常兜底存档。客户端断开导致生成器被取消时 finally 里不能再
            # await，存档甩成独立任务挂在事件循环上自行跑完。只存"安全前缀"——
            # 收尾不完整的一轮（tool_calls 缺 tool 回应）整段丢弃；已流式给用户
            # 看过、尚未定稿进 messages 的半截文本单独补存。_persist_messages 单
            # 事务失败即整体回滚，与分步提交的批次不会产生重复消息。
            if committed < len(messages) or streamed_text:
                msgs = _safe_prefix(messages[committed:])
                if streamed_text and not any(
                    m.get("role") == "assistant" and m.get("content") == streamed_text
                    for m in msgs
                ):
                    msgs.append({"role": "assistant", "content": streamed_text})
                if msgs:
                    _spawn_persist(conv.id, msgs)

    return EventSourceResponse(event_generator())


# ═══════════════════════════════════════════════════════════
#  确认接口
# ═══════════════════════════════════════════════════════════

@router.post("/confirm")
async def agent_confirm(body: ConfirmRequest):
    """确认或拒绝危险操作（写入 Redis，由发起对话的 worker 轮询读取）"""
    if not await check_token(body.user, body.token):
        return {"code": 4}

    key = f"{CONFIRM_KEY_PREFIX}{body.confirmation_token}"
    raw = await redis.get(key)
    if not raw:
        return {"code": 1, "msg": "confirmation token expired or invalid"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"code": 1, "msg": "confirmation record corrupted"}

    # 确认令牌必须归属当前请求用户
    if data.get("user") != body.user:
        return {"code": 1, "msg": "confirmation token does not belong to this user"}

    if body.decision not in ("approved", "rejected"):
        return {"code": 1, "msg": "decision must be approved or rejected"}

    data["decision"] = body.decision
    await redis.setex(key, CONFIRM_TTL, json.dumps(data, ensure_ascii=False))
    return {"code": 0, "msg": body.decision}


# ═══════════════════════════════════════════════════════════
#  历史会话接口
# ═══════════════════════════════════════════════════════════

@router.post("/conversations")
async def list_conversations(body: ConversationListRequest):
    """历史会话列表（按最近更新倒序，仅返回元数据不含消息正文）"""
    if not await check_token(body.user, body.token):
        return {"code": 4}

    # 列表展示的条数只统计用户发送的消息（问答轮次），
    # 不含 AI 回复与工具消息；message_count 保留全量口径仅供内部统计
    user_msg_count = (
        select(func.count())
        .where(
            AgentMessage.conv_id == AgentConversation.id,
            AgentMessage.role == "user",
        )
        .correlate(AgentConversation)
        .scalar_subquery()
    )
    async with Session() as db:
        cond = (
            AgentConversation.user == body.user,
            AgentConversation.deleted_at.is_(None),
        )
        rows = (await db.execute(
            select(AgentConversation, user_msg_count)
            .where(*cond)
            .order_by(AgentConversation.update_time.desc(), AgentConversation.id.desc())
            .offset(max(0, body.start))
            .limit(max(1, min(body.count, 100)))
        )).all()
        total = (await db.execute(
            select(func.count()).select_from(AgentConversation).where(*cond)
        )).scalar() or 0
    return {
        "code": 0,
        "total": total,
        "conversations": [
            {
                "conv_id": r.conv_key,
                "title": r.title,
                "message_count": r.message_count,
                "user_message_count": uc or 0,
                "update_time": r.update_time.strftime("%Y-%m-%d %H:%M") if r.update_time else "",
            }
            for r, uc in rows
        ],
    }


@router.post("/conversations/messages")
async def conversation_messages(body: ConversationMessagesRequest):
    """读取会话消息：默认返回最新一页（正序），传 before_seq 取更早一页"""
    if not await check_token(body.user, body.token):
        return {"code": 4}
    async with Session() as db:
        conv = (await db.execute(
            select(AgentConversation).where(
                AgentConversation.conv_key == body.conv_id,
                AgentConversation.user == body.user,
                AgentConversation.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not conv:
            return {"code": 1, "msg": "会话不存在"}

        q = select(AgentMessage).where(AgentMessage.conv_id == conv.id)
        if body.before_seq is not None:
            q = q.where(AgentMessage.seq < body.before_seq)
        q = q.order_by(AgentMessage.seq.desc()).limit(max(1, min(body.count, 200)))
        rows = list(reversed((await db.execute(q)).scalars().all()))  # 倒序取页后转正序

    return {
        "code": 0,
        "conversation": {
            "conv_id": conv.conv_key,
            "title": conv.title,
            "message_count": conv.message_count,
        },
        "messages": [
            {
                "seq": r.seq,
                "role": r.role,
                "content": r.content or "",
                "tool_calls": r.tool_calls or [],
                "tool_call_id": r.tool_call_id or "",
                "create_time": r.create_time.strftime("%Y-%m-%d %H:%M") if r.create_time else "",
            }
            for r in rows
        ],
        # seq 从 1 连续分配，最小 seq > 1 即还有更早消息
        "has_older": bool(rows) and rows[0].seq > 1,
    }


@router.post("/conversations/delete")
async def delete_conversation(body: ConversationDeleteRequest):
    """软删除会话：列表立即不可见，消息保留供后续审计/恢复"""
    if not await check_token(body.user, body.token):
        return {"code": 4}
    async with Session() as db:
        conv = (await db.execute(
            select(AgentConversation).where(
                AgentConversation.conv_key == body.conv_id,
                AgentConversation.user == body.user,
                AgentConversation.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not conv:
            return {"code": 1, "msg": "会话不存在"}
        conv.deleted_at = func.now()
        await db.commit()
    return {"code": 0}


async def _sse(event_type: str, data: dict) -> dict:
    """构造 SSE 事件字典"""
    return {"event": event_type, "data": json.dumps(data, ensure_ascii=False)}


async def _error_stream(msg: str):
    """返回错误 SSE 流"""
    yield {"event": "error", "data": json.dumps({"message": msg}, ensure_ascii=False)}
    yield {"event": "done", "data": json.dumps({"conv_id": "", "total_tool_calls": 0}, ensure_ascii=False)}
