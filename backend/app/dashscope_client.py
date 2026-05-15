"""
DashScope API 封装 —— 阿里百炼模型调用

三个能力:
  1. describe_image  —— 图片描述（原生多模态生成 API）
  2. get_embedding   —— 文本向量化（原生 text-embedding API）
  3. create_client   —— 返回 OpenAI 兼容客户端（供 Agent chat 用）

describe_image 和 get_embedding 使用 DashScope 原生 API（httpx 直调），
create_client 使用 OpenAI 兼容端点（/compatible-mode/v1）。
所有可配置参数统一由 config.Settings 管理，不在此模块硬编码。
"""
import httpx
from openai import AsyncOpenAI
from app.config import settings


def create_client(api_key: str) -> AsyncOpenAI:
    """创建指向 DashScope 兼容端点的 OpenAI 客户端（用于 Agent chat + embedding）"""
    return AsyncOpenAI(api_key=api_key, base_url=settings.dashscope_api_base)


async def describe_image(api_key: str, image_url: str) -> str:
    """
    调用 Qwen-VL 多模态模型，传入图片公网 URL，返回中文图片描述。
    使用 httpx 直调 DashScope 多模态生成 API。
    网络异常或 API 错误时返回降级描述文案。
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
    except Exception as exc:
        return "无法描述此图片"

    # 响应路径: output.choices[0].message.content[0].text
    try:
        content = data["output"]["choices"][0]["message"]["content"]
        if isinstance(content, list):
            return content[0].get("text", "无法描述此图片")
        return content
    except (KeyError, IndexError, TypeError):
        return "无法描述此图片"


async def get_embedding(api_key: str, text: str) -> list[float]:
    """
    调用 DashScope 原生文本向量化 API，将文本转为浮点向量。
    使用 httpx 直调原生接口（不走 OpenAI 兼容端点），支持所有原生模型名。
    失败时抛出 RuntimeError，由调用方统一处理。
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
    except Exception as exc:
        raise RuntimeError(f"向量化 API 请求失败: {exc}") from exc

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
