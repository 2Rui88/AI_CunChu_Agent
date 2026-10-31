# AI 云存储系统 (AI-YunCunChu)

一个支持自然语言搜索的私有云文件管理系统。你可以像使用网盘一样上传文件，然后像问朋友一样查找它们——输入"帮我找一下上个月的技术方案"，系统会自动理解你的意图、找到最相关的文件片段，并用 AI 助手以对话方式呈现结果。

### 解决了什么问题

传统文件系统只能按文件名或文件夹查找。文件一多，名字记不住就找不到。这个项目做的就是把文件内容"读懂"后存成向量，用语义匹配代替关键词匹配——你描述内容，它来匹配。

### 核心能力

| 功能 | 说明 |
|------|------|
| **文件管理** | 上传、秒传、分片上传、分享、删除，标准云盘功能 |
| **AI 语义搜索** | 上传的文档自动提取内容 → 智能分块 → 向量化，支持自然语言搜索（入口整合在 AI 助手对话中） |
| **多格式解析** | 支持 PDF、Word、Excel、Markdown、代码等 7 种文件格式的内容提取 |
| **AI 文件助手** | 对话式交互，全站唯一搜索入口；可以搜索文件、查看详情、分享、删除，危险操作需确认 |

### 一个典型的使用流程

```
上传 "2025年度技术方案.docx"
  → 系统自动提取内容，按章节分块
  → 逐块生成 768 维向量，存入 FAISS 索引

三个月后，你在 AI 助手对话中输入: "帮我看一下那个关于微服务架构的方案"
  → 一次 LLM 查询规划：判断模糊/精确，生成改写查询与文件名关键词
  → FAISS 向量召回 + MySQL 关键词召回，按文件合并后统一 RRF 排序
  → 候选 >5 时 LLM 重排序
  → 返回: "找到了「2025年度技术方案」的第3章 微服务架构设计"
```

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + Ant Design 5 + Emotion |
| 后端 | Python 3.12 + FastAPI + Gunicorn |
| AI 推理 | 阿里百炼 DashScope (Qwen3.5 / embedding) |
| 向量检索 | FAISS IndexFlatIP (768 维) |
| 文件存储 | MinIO (S3 兼容对象存储) |
| 数据库 | MySQL 8.0 |
| 缓存 | Redis 7 |
| Web 服务器 | Nginx 1.26 (SSL 终端 + 反向代理) |
| 容器化 | Docker Compose |
| 文档解析 | PyMuPDF (PDF) + openpyxl (Excel)，docx 经 ZIP+XML 直接提取 |

## 功能说明

### 文件管理

- **上传与秒传**：上传前先计算文件 MD5 发送给服务端，若目标文件已存在则直接引用，无需重复传输；服务端会复核内容 MD5，防止伪造
- **分片上传**：大文件自动拆分为分片，支持断点续传——网络中断后重传只需补传缺失分片（分片临时目录挂 Docker 卷，容器重启也不丢）
- **文件列表**：按上传时间降序展示，支持分页；未建立内容索引的文件（超体积门槛或提取失败）可手动重建索引
- **文件下载**：限时预签名 URL 直连 MinIO（默认 2 小时有效），不经过后端进程；桶为私有，链接过期即失效
- **删除保护**：同一文件被多个用户引用时，仅删除自己的引用，物理文件在最后一个引用者删除时清理（引用计数带行级锁，防并发丢失更新）

### 分享与排行

- **共享广场**：文件分享后出现在共享广场，其他登录用户可转存到自己的存储空间（仅限确已共享的文件）
- **下载排行**：仅统计共享广场的下载（文件页/图片库的下载不计入），MySQL 为真值来源，Redis ZSET 作排行榜缓存，缓存丢失自动懒重建
- **取消分享**：分享者可以随时撤销，同时清理 Redis 和共享列表

### AI 文件理解

- **多模态描述**：图片上传后调用 Qwen-VL 生成中文描述，文本文件直接提取内容
- **多格式解析**：PDF（逐页提取+字号检测）、Excel（按工作表+行解析）、Word（解压 XML 提取）、Markdown（标题识别）、代码（函数/类声明识别）
- **自适应分块**：按文件结构切分——Markdown 按 `##` 标题、代码按函数声明、PDF 按页面、Excel 按工作表，每个切片保留上下文标签
- **两级缓存**：`file_ai_desc` 全局缓存（跨用户复用）+ `user_file_ai_desc` 用户记录，同一文件只需一次 AI 调用
- **密钥统一管理**：API Key 仅由部署者配置在环境变量 `DASHSCOPE_API_KEY`，所有用户复用；不落库、不下发前端、不出现在任何响应中

### AI 语义搜索

- **唯一搜索入口**：首页独立搜索框与 `/api/ai/search` 端点已移除，语义搜索全链路由 AI 助手的 `search_files` 工具委托 `semantic_search` 完成——一次 LLM 查询规划 + 双路召回 + 统一 RRF + 候选>5 时 LLM 重排
- **自然语言查询**：在助手对话中描述文件内容而非记忆文件名，如"帮我找一下上个月的技术方案"
- **一次 LLM 查询规划**：单次 LLM 调用同时完成查询类型判断（模糊/精确）、向量改写查询（模糊生成 3 个同义变体取均值向量）、文件名关键词生成，替代原先"分类 + 改写"两次调用；程序侧不再用正则判断查询类型
- **双路混合召回**：向量语义匹配（FAISS）+ 关键词精确匹配（MySQL LIKE，关键词由 LLM 规划生成，为空则跳过该路）；按文件 md5 合并候选，统一 RRF 排序（谁有结果用谁，两路都有则分数相加）
- **LLM 重排序**：候选数 >5 时取 Top-20 经大模型精排至 Top-10（失败回退 RRF 顺序）；候选 ≤5 直接按 RRF 返回，省一次秒级 LLM 调用
- **结果溯源**：返回 match_context 标注命中位置（如"第3章 微服务架构"）

### 索引校验（向量化文档页）

- **只读校验页** `/vector-docs`：按 MD5 聚合展示已完成 AI 描述、切分、向量化的文档，便于验证语义索引链路
- **切块明细下钻**：可查看每个文档的切块列表（faiss_id、向量状态、降级存根标记），定位"描述生成了但向量没入库"之类的链路问题
- **批量测试脚本** `testdocs/`：生成 8 份互不重复的 3500+ 字 docx，上传后经只读接口全量校验切片/向量化零丢失

### AI 文件助手

- **对话式交互**：SSE 实时流式输出，多轮上下文（用户消息先落库，连接中断也不丢提问）
- **历史会话持久化**：会话元数据与全量消息存 MySQL（agent_conversation / agent_message），左侧栏可查看、恢复、删除历史会话；组装 LLM 上下文时按 user 消息边界截断，工具调用对不被切断
- **7 个工具函数**：search_files、get_file_info、list_recent_files、get_storage_stats、describe_file、delete_file、share_file
- **三级安全模型**：LLM 自主判断（提示词引导）→ 工具级门控（暂停+确认弹窗）→ 服务端鉴权（校验文件归属）
- **危险操作确认**：删除/分享操作必须用户在前端弹窗中确认，5 分钟超时自动取消

### 索引维护

- **脏标记自动重建**：文件删除时标记 dirty，下次搜索前自动从 MySQL 全量重建 FAISS 索引
- **文件锁并发控制**：flock 排他锁，防止多个搜索请求同时触发重建
- **维度自动适配**：切换 embedding 模型时检测到索引维度变化，自动删除旧索引重建

## RAG 检索系统

### 摄入层

- **多格式文本提取**：支持 PDF、Excel、Word、Markdown、代码等 7 类文件的内容提取；提取设三层策略——体积安全线 100 MB（手动重建索引时放宽到 1 GB 硬上限）+ 统一字符预算 30000（所有类型收满即停，交给分块器切分）
- **自适应分块**：按文件结构（章节标题、工作表、函数声明、页面分组）智能切分，每个切片保留上下文标签（如"第三章 核心方案"）
- **逐块向量化**：每个切片独立调用 embedding API 生成 768 维向量，存入 FAISS 索引

### 检索层

- **查询规划**：一次 LLM 调用返回 query_type + vector_queries（模糊=3 个改写取均值向量）+ mysql_keywords（精确查询的文件名锚点词），结构校验失败安全降级
- **双路混合召回**：向量语义搜索（FAISS）+ 关键词精确匹配（MySQL LIKE，关键词由 LLM 规划），按 md5 合并统一 RRF（K=60）排序
- **LLM 重排序**：候选数 >5 时 Top-20 精排至 Top-10，≤5 跳过

### 维护与一致性

- **脏标记自动重建**：文件删除后标记 dirty，下次搜索前自动从 MySQL 全量重建 FAISS 索引
- **文件锁并发控制**：防止多个搜索请求同时触发重建；add_vector/rebuild 均持用户级文件锁（跨 worker 串行化），索引落盘为原子替换

## 快速开始

### 环境要求

- Docker Desktop 20.10+
- Node.js 18+ (前端开发)
- 阿里百炼 DashScope API Key

### 一键启动

```bash
# 1. 克隆项目
git clone <repo-url> && cd AI_CunChu_Agent

# 2. 配置环境变量
cp docker/.env.example docker/.env
#    编辑 docker/.env：
#    - 修改 MySQL/MinIO 密码（必填）
#    - PUBLIC_SERVER_IP 设为服务器 IP/域名（生成预签名下载链接必需）
#    - DASHSCOPE_API_KEY 填阿里百炼 API Key（AI 功能统一密钥，可选）

# 3. 启动所有服务（前端已内置到 Nginx 镜像的多阶段构建中，无需单独构建）
cd docker && docker compose up -d --build

# 4. 存量数据库执行迁移（新数据卷跳过；按需执行，新装部署只跑未执行过的）
docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < mysql/migration_bcrypt.sql
docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < mysql/migration_chunk.sql
docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < mysql/migration_share_unique.sql
docker exec -i tc_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu < mysql/migration_drop_apikey.sql

# 5. 访问
open https://localhost
```

### 首次使用

1. 打开 `https://localhost`（自签名证书需手动信任）
2. 注册账号并登录
3. 上传文件后在首页 AI 助手对话中即可语义搜索、查看和管理文件

## 项目结构

```
AI_CunChu_Agent/
├── backend/                        # Python 后端
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── config.py               # 配置管理（支持环境变量）
│   │   ├── dependencies.py         # Token 验证
│   │   ├── database.py             # MySQL 异步连接池
│   │   ├── redis_client.py         # Redis 客户端
│   │   ├── models.py               # SQLAlchemy ORM (9 张表)
│   │   ├── file_ops.py             # 文件-用户关联写入（并发上传竞态安全补建）
│   │   ├── minio_client.py         # MinIO 对象存储客户端
│   │   ├── dashscope_client.py     # DashScope API 封装（原生+兼容端点）
│   │   ├── faiss_service.py        # FAISS 向量索引 + 脏标记 + 文件锁
│   │   ├── chunker.py              # 7 类文件自适应分块器
│   │   ├── agent_tools.py          # Agent 7 个工具函数（search_files 委托 semantic_search）
│   │   └── routers/
│   │       ├── auth.py             # 注册/登录（bcrypt + 限流）
│   │       ├── files.py            # 上传/秒传/文件列表
│   │       ├── chunk.py            # 大文件分片上传
│   │       ├── share.py            # 分享/删除/转存/排行
│   │       ├── ai.py               # AI 描述/重建（含分块+Rerank；搜索端点已并入 Agent）
│   │       ├── agent.py            # Agent SSE 对话 + 确认门控 + 会话历史
│   │       └── vector_docs.py      # 向量化文档只读校验接口
│   ├── gunicorn.conf.py            # 生产配置
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                       # React 前端
│   └── src/
│       ├── components/             # AppSidebar / PageHeader / AssistantChat / ConfirmModal
│       ├── pages/                  # Login / Home / FileList / ImageList / SharedFiles / TopDownloads / VectorDocs
│       ├── services/               # auth.js / images.js / ai.js / agent.js / vectorDocs.js
│       └── config/index.js         # API 端点配置
├── docker/
│   ├── docker-compose.yaml         # 5 容器编排（前端经多阶段构建内置入 Nginx 镜像）
│   ├── .env.example                # 环境变量模板（复制为 .env，不入 Git）
│   ├── nginx/                      # Nginx 配置 + Let's Encrypt + SSL
│   └── mysql/                      # 建表 SQL + 迁移脚本
├── testdocs/                       # 批量测试文档生成与上传校验脚本（out/ 产物不入库）
├── RAG_UPGRADE_DESIGN.md           # RAG 检索升级设计
├── RAG_UPGRADE_PROGRESS.md         # RAG 升级进度跟踪
├── IMPLEMENTATION_STRATEGY.md      # 实现思路
└── PROGRESS.md                     # 实现进度跟踪
```

## 容器架构

```
浏览器 HTTPS → Nginx (443)
                  ├── /             → 前端静态资源
                  ├── /api/         → FastAPI 后端 (8000)
                  ├── /api/agent/   → Agent SSE (8000，关闭缓冲 + 600s 读超时)
                  └── /dl/          → MinIO (9000，预签名下载代理)

tc_mysql   :3306     MySQL 8.0（时区固定 +08:00）
tc_redis   :6379     Redis 7
tc_minio   :9000     MinIO 对象存储
tc_backend :8000     Gunicorn (8 workers) + FastAPI
tc_nginx   :443/80   Nginx 反向代理
```

## API 概览

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/reg` | POST | 用户注册 |
| `/api/login` | POST | 用户登录，返回 Token |
| `/api/upload` | POST | 文件上传 (multipart) |
| `/api/md5` | POST | 秒传检测 |
| `/api/myfiles?cmd=normal` | POST | 文件列表 |
| `/api/dealfile?cmd=share\|del\|pv` | POST | 分享/删除/下载计数 |
| `/api/sharefiles?cmd=normal\|pvdesc` | POST | 共享广场/下载排行 |
| `/api/dealsharefile?cmd=save\|cancel\|pv` | POST | 转存/取消分享 |
| `/api/chunk_init` | POST | 分片上传初始化 |
| `/api/chunk_upload` | POST | 分片上传 |
| `/api/chunk_merge` | POST | 分片合并 |
| `/api/ai/describe` | POST | AI 文件描述生成（自适应分块） |
| `/api/ai/rebuild` | POST | 重建 FAISS 索引 |
| `/api/vector/docs` | POST | 向量化文档聚合列表（只读校验） |
| `/api/vector/chunks` | POST | 文档切块明细（只读校验） |
| `/api/agent/chat` | POST | Agent SSE 对话（含全站语义搜索） |
| `/api/agent/confirm` | POST | 确认危险操作 |
| `/api/agent/conversations` | POST | 历史会话列表（分页） |
| `/api/agent/conversations/messages` | POST | 读取会话消息（支持 before_seq 向上翻页） |
| `/api/agent/conversations/delete` | POST | 软删除会话 |

## 配置说明

核心配置文件 `backend/app/config.py`，支持环境变量覆盖：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `embedding_model` | tongyi-embedding-vision-flash-2026-03-06 | 向量化模型 |
| `vl_model` | qwen3.8-omni-flash | 视觉理解模型（全模态统一价，图片描述成本约为上代的 1/20；可用环境变量回退 qwen3.5-omni-flash） |
| `chat_model` | qwen3.5-flash | Agent 对话模型 |
| `embedding_dimension` | 768 | 向量维度 |
| `embedding_max_chars` | 3000 | 单切片最大字符数（分块阈值） |
| `parse_max_mb` | 100 | 常规内容提取的文件体积安全线（MB），超过退化为文件名存根 |
| `parse_max_mb_forced` | 1024 | 手动"建立内容索引"时的硬上限（MB） |
| `extract_char_budget` | 30000 | 统一字符预算：所有类型提取收满即停，交给分块器切分 |
| `public_server_ip` | (空) | 公网 IP/域名（预签名下载链接与 DashScope 拉图必需） |
| `public_server_ssl` | true | 对外地址是否为 HTTPS |
| `dashscope_api_key` | (空) | AI 统一密钥（环境变量 DASHSCOPE_API_KEY，不落库不下发） |
| `max_context_messages` | 20 | 组装 LLM 上下文的最大消息条数（按 user 消息边界截断） |
| `max_tool_calls_per_turn` | 5 | 单轮对话允许的最大工具调用次数 |

## 部署上线

1. 购买云服务器（建议 4 核 8 GB）
2. 安装 Docker Desktop
3. `docker/.env` 中将 `PUBLIC_SERVER_IP` 设为服务器公网 IP/域名
4. 执行 `docker/nginx/setup-certbot.sh 你的域名` 获取 SSL 证书
5. `docker compose up -d --build`
6. 存量数据库按顺序执行 `docker/mysql/` 下的迁移脚本
