from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── MySQL ──
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_db: str = "yuncunchu"
    # 连接池（角色分池：HTTP 池默认 10+20；任务池由 compose 收紧为 8+12，
    # 两池合计不超过 MySQL max_connections 的安全水位）
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 20

    # ── Redis ──
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ── MinIO ──
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "files"

    # ── DashScope ──
    # 统一服务端密钥：仅在部署环境变量（DASHSCOPE_API_KEY）中配置，由后端自取，
    # 不落库、不下发前端、不出现在任何 API 响应中
    dashscope_api_key: str = ""
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
    # VL：qwen3.8-omni-flash（2026-09 上线）所有模态输入统一 0.8 元/百万token、输出 2.7，
    #     纯图片理解场景下比 qwen3.5-omni-flash（图片输入 18 元/输出 13.3 元）便宜约 95%；
    #     如遇调用不兼容，用环境变量 VL_MODEL=qwen3.5-omni-flash 即时回退
    vl_model: str = "qwen3.8-omni-flash"                                   # 视觉理解模型
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
    # 对话历史已落 MySQL（agent_conversation/agent_message），不再有 Redis TTL
    max_context_messages: int = 20
    max_tool_calls_per_turn: int = 5
    # read_file 工具（分页读取文本文件；单页结果会进 LLM 上下文并落库，预算须比
    # extract_char_budget 紧）
    read_file_default_chars: int = 4000    # 默认页大小（字符）
    read_file_max_chars: int = 12000       # 单次请求字符上限
    read_file_min_chars: int = 500         # 页大小钳制下限
    read_file_slack_bytes: int = 4096      # 行对齐扫描余量（字节）

    # ── 向量化任务队列（indexing_jobs）──
    # 上传只投递任务立即返回；重活（下载/提取/描述/向量化）由后台消费者执行。
    # 角色分化：INDEXING_WORKER_ENABLED=0 的进程（HTTP 池）不跑消费循环，
    # 任务池专职消费；全局消费并发 = 消费进程数 × indexing_concurrency
    indexing_worker_enabled: bool = True    # 本进程是否运行消费循环（灰度期默认开）
    indexing_concurrency: int = 4           # 单进程任务槽（先占槽再领任务）
    indexing_poll_seconds: float = 2.0      # 队列空时的轮询间隔（秒）
    indexing_job_timeout_seconds: int = 900 # running 超时视为消费者崩溃，回收重投
    indexing_max_attempts: int = 3          # 失败自动重试上限（含首次执行）

    # ── DashScope 全局并发闸门（跨进程，Redis ZSET 计数）──
    # 共享 API Key 的保护层：embedding/chat/vl 各自限并行，超出排队；
    # 429/网络抖动按指数退避重试。进程内信号量只能管单进程，这里必须跨进程
    gate_embedding_limit: int = 5            # embedding 并行上限
    gate_chat_limit: int = 3                 # chat / 查询规划 / 重排并行上限
    gate_vl_limit: int = 3                   # VL 图片描述并行上限
    gate_lease_seconds: int = 300            # 闸门持有兜底 TTL（持有者崩溃后自动放行）
    gate_wait_seconds: int = 20              # 闸门内最长排队等待，超时快速失败
    gate_backoff_attempts: int = 3           # 可重试错误的退避重试次数
    gate_backoff_base_seconds: float = 1.0   # 指数退避基数（1s/2s/4s + 抖动）

    # ── CORS ──
    # 允许跨域的前端来源（逗号分隔）。生产同源部署（Nginx 同域反代）无需跨域；
    # 默认放行本地开发常用地址，正式环境用环境变量 CORS_ORIGINS 覆盖。
    cors_origins: str = (
        "http://localhost:3000,http://localhost:3456,"
        "http://127.0.0.1:3000,http://127.0.0.1:3456"
    )

    model_config = {"env_file": ".env"}


settings = Settings()
