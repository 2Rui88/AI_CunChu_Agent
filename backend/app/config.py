from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── MySQL ──
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_db: str = "yuncunchu"

    # ── Redis ──
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ── MinIO ──
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "files"

    # ── DashScope ──
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 多模态生成（Qwen-VL 图片描述）
    dashscope_vl_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/"
        "aigc/multimodal-generation/generation"
    )
    # 模型名称
    vl_model: str = "qwen3.5-omni-flash"                                     # 视觉理解模型
    embedding_model: str = "tongyi-embedding-vision-flash-2026-03-06"   # 文本向量化模型
    chat_model: str = "qwen3.5-flash"                                    # Agent 对话模型
    # 参数
    embedding_dimension: int = 1024                # 向量维度
    vl_timeout: int = 60                           # Qwen-VL 请求超时秒数
    vl_prompt: str = (
        "请用中文详细描述这张图片的内容，"
        "包括主要物体、场景、颜色、文字等信息。"
    )

    # ── 服务器地址（供 DashScope 回调下载图片）──
    web_server_ip: str = "172.30.0.5"    # Nginx 容器 IP（Docker 内网，供后端下载文本文件）
    web_server_port: str = "443"          # Nginx HTTPS 端口
    public_server_ip: str = ""            # 公网 IP（DashScope 可访问，部署时填写）
    public_server_port: str = "8080"      # 公网端口（如使用 Nginx 反向代理则为 443）

    # ── FAISS ──
    faiss_user_index_dir: str = "/data/faiss/users"

    # ── Agent ──
    conversation_ttl_seconds: int = 3600
    max_context_messages: int = 20
    max_tool_calls_per_turn: int = 5

    model_config = {"env_file": ".env"}


settings = Settings()
