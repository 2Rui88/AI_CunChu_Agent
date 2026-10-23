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
    # 原生 API 端点（不走 OpenAI 兼容接口）
    dashscope_vl_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/"
        "aigc/multimodal-generation/generation"
    )
    dashscope_emb_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/"
        "embeddings/multimodal-embedding/multimodal-embedding"
    )
    # 模型名称
    vl_model: str = "qwen3.5-omni-flash"                                     # 视觉理解模型
    embedding_model: str = "tongyi-embedding-vision-flash-2026-03-06"   # 文本向量化模型
    chat_model: str = "qwen3.5-flash"                                    # Agent 对话模型
    # 参数
    embedding_dimension: int = 768                 # 向量维度（tongyi-embedding-vision-flash 模型为 768）
    embedding_max_chars: int = 3000               # 单切片最大字符数（分块阈值，与模型 token 上限对齐）
    vl_timeout: int = 60                           # Qwen-VL 请求超时秒数
    # ── 内容提取三层策略 ──
    # 第 1 层：解析安全线（体积门槛只管"读起来安不安全"，不管"值不值得索引"）
    parse_max_mb: int = 100                        # 常规提取允许的最大文件体积（MB）
    parse_max_mb_forced: int = 1024                # 手动"建立内容索引"时的硬上限（MB）
    # 第 2 层：统一字符预算（真正的索引成本线，所有类型收满即停、交给分块器切分）
    extract_char_budget: int = 30000
    vl_prompt: str = (
        "请用中文详细描述这张图片的内容，"
        "包括主要物体、场景、颜色、文字等信息。"
    )

    # ── 服务器地址（供生成对外预签名下载 URL）──
    web_server_ip: str = "172.30.0.5"    # Nginx 容器 IP（Docker 内网，供后端下载文本文件）
    web_server_port: str = "443"          # Nginx HTTPS 端口
    public_server_ip: str = ""            # 公网 IP/域名（浏览器与 DashScope 可达；为空则退回 minio_endpoint 直连）
    public_server_port: str = "443"       # 公网端口（默认走 Nginx 443）
    public_server_ssl: bool = True        # 对外地址是否为 HTTPS（Nginx 默认 443 为是）

    # ── FAISS ──
    faiss_user_index_dir: str = "/data/faiss/users"

    # ── Agent ──
    conversation_ttl_seconds: int = 3600
    max_context_messages: int = 20
    max_tool_calls_per_turn: int = 5

    # ── CORS ──
    # 允许跨域的前端来源（逗号分隔）。生产同源部署（Nginx 同域反代）无需跨域；
    # 默认放行本地开发常用地址，正式环境用环境变量 CORS_ORIGINS 覆盖。
    cors_origins: str = (
        "http://localhost:3000,http://localhost:3456,"
        "http://127.0.0.1:3000,http://127.0.0.1:3456"
    )

    model_config = {"env_file": ".env"}


settings = Settings()
