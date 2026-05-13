"""
DashScope API 封装 —— 阿里百炼模型调用

三个能力:
  1. describe_image  —— Qwen-VL 图片描述（多模态生成 API）
  2. get_embedding   —— text-embedding-v3 文本向量化
  3. create_client   —— 返回 OpenAI 兼容客户端（供 Agent 聊天用）
"""
import httpx
from openai import AsyncOpenAI
from app.config import settings

# API 端点
VL_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


def create_client(api_key: str) -> AsyncOpenAI:
    """创建指向 DashScope 兼容端点的 OpenAI 客户端（用于 Agent chat + embedding）"""
    return AsyncOpenAI(api_key=api_key, base_url=settings.dashscope_api_base)


async def describe_image(api_key: str, image_url: str) -> str:
    """
    调用 Qwen-VL 多模态模型，传入图片公网 URL，返回中文图片描述。
    直接使用 httpx 调用 DashScope 多模态生成 API（不走 OpenAI 兼容接口，
    因为 Qwen-VL 的多模态消息格式与 OpenAI Chat 格式不同）。
    """
    body = {
        "model": "qwen-vl-plus",
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": "请用中文详细描述这张图片的内容，包括主要物体、场景、颜色、文字等信息。"},
                ],
            }],
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            VL_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        data = resp.json()

    # 路径: output.choices[0].message.content[0].text
    try:
        content = data["output"]["choices"][0]["message"]["content"]
        if isinstance(content, list):
            return content[0].get("text", "无法描述此图片")
        return content
    except (KeyError, IndexError, TypeError):
        return "无法描述此图片"


async def get_embedding(api_key: str, text: str) -> list[float]:
    """
    调用 text-embedding-v3 将文本转为 1024 维浮点向量。
    使用 OpenAI 兼容端点。
    """
    client = create_client(api_key)
    resp = await client.embeddings.create(
        model="text-embedding-v3",
        input=text,
        dimensions=settings.embedding_dimension,
    )
    return resp.data[0].embedding
