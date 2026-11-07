"""
DashScope API 封装 —— 阿里百炼模型调用

三个能力:
  1. describe_image  —— 图片描述（原生多模态生成 API）
  2. get_embedding   —— 文本向量化（原生 text-embedding API）
  3. create_client   —— 返回 OpenAI 兼容客户端（供 Agent chat 用）

describe_image 和 get_embedding 使用 DashScope 原生 API（httpx 直调），
create_client 使用 OpenAI 兼容端点（/compatible-mode/v1）。
所有可配置参数统一由 config.Settings 管理，不在此模块硬编码。

所有出站调用统一经过 ratelimit 的跨进程并发闸门（多 worker 部署下保护
共享 API Key），并对限流（429/Throttling）与网络抖动做指数退避重试；
重试耗尽后维持原有失败语义（embedding 抛 RuntimeError / VL 返回降级文案），
调用方处理方式不变。
"""
import httpx
from openai import AsyncOpenAI

from app import ratelimit
from app.config import settings
from app.ratelimit import Gate, RetryableError, call_with_backoff

# 进程级客户端缓存：AsyncOpenAI 内含 httpx 连接池，每请求新建从不关闭会累积
# 未释放连接；同 key 复用同一实例，连接池随进程生命周期管理。
_client_cache: dict[str, AsyncOpenAI] = {}


def create_client(api_key: str) -> AsyncOpenAI:
    """返回指向 DashScope 兼容端点的 OpenAI 客户端（Agent chat + embedding 共用，按 key 缓存）"""
    client = _client_cache.get(api_key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=settings.dashscope_api_base)
        _client_cache[api_key] = client
    return client


def _throttled(status_code: int | None, data: dict) -> bool:
    """判定 DashScope 原生 API 的限流响应（HTTP 429 或业务 code 含 Throttling）"""
    if status_code == 429:
        return True
    code = str(data.get("code") or "")
    return "Throttling" in code or "RateLimit" in code.lower()


async def describe_image(api_key: str, image_url: str) -> str:
    """
    调用 Qwen-VL 多模态模型，传入图片公网 URL，返回中文图片描述。
    使用 httpx 直调 DashScope 多模态生成 API。
    限流/网络错误退避重试；仍失败时返回降级描述文案。
    """
    body = {
        "model": settings.vl_model,
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": settings.vl_prompt},
                ],
            }],
        },
    }

    async def _call() -> str:
        try:
            async with httpx.AsyncClient(timeout=settings.vl_timeout) as client:
                resp = await client.post(
                    settings.dashscope_vl_url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                data = resp.json()
        except httpx.RequestError as exc:
            raise RetryableError(f"vl network error: {exc}") from exc
        if _throttled(resp.status_code, data):
            raise RetryableError(f"vl throttled: {data.get('code', resp.status_code)}")

        # 响应路径: output.choices[0].message.content[0].text
        try:
            content = data["output"]["choices"][0]["message"]["content"]
            if isinstance(content, list):
                return content[0].get("text", "无法描述此图片")
            return content
        except (KeyError, IndexError, TypeError):
            return "无法描述此图片"

    try:
        async with Gate(ratelimit.GATE_VL):
            return await call_with_backoff(_call)
    except Exception:
        # 退避耗尽 / 响应异常：降级文案（由调用方按存根处理）
        return "无法描述此图片"


async def get_embedding(api_key: str, text: str) -> list[float]:
    """
    调用 DashScope 原生文本向量化 API，将文本转为浮点向量。
    使用 httpx 直调原生接口（不走 OpenAI 兼容端点），支持所有原生模型名。
    限流/网络错误退避重试；仍失败时抛 RuntimeError，由调用方统一处理。
    """
    body = {
        "model": settings.embedding_model,
        "input": {
            "contents": [{"text": text}],
        },
        "parameters": {},
    }
    if settings.embedding_dimension:
        body["parameters"]["dimension"] = settings.embedding_dimension

    async def _call() -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    settings.dashscope_emb_url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                data = resp.json()
        except httpx.RequestError as exc:
            raise RetryableError(f"embedding network error: {exc}") from exc
        if _throttled(resp.status_code, data):
            raise RetryableError(
                f"embedding throttled: {data.get('code', resp.status_code)}")
        # 检查 API 错误码
        if data.get("code") and data.get("code") != "":
            raise RuntimeError(
                f"向量化 API 返回错误: code={data.get('code')}, "
                f"message={data.get('message', '')}"
            )
        # 响应路径: output.embeddings[0].embedding
        try:
            return data["output"]["embeddings"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"向量化响应解析失败: 不支持的响应格式") from exc

    try:
        async with Gate(ratelimit.GATE_EMBEDDING):
            return await call_with_backoff(_call)
    except RetryableError as exc:
        # _call 内网络/限流统一转可重试；这里是退避耗尽后的最终失败
        raise RuntimeError(f"向量化 API 请求失败: {exc}") from exc
