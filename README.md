# AI 云存储系统 (AI-YunCunChu)

一个支持自然语言搜索的私有云文件管理系统。你可以像使用网盘一样上传文件，然后像问朋友一样查找它们——输入"帮我找一下上个月的技术方案"，系统会自动理解你的意图、找到最相关的文件片段，并用 AI 助手以对话方式呈现结果。

### 解决了什么问题

传统文件系统只能按文件名或文件夹查找。文件一多，名字记不住就找不到。这个项目做的就是把文件内容"读懂"后存成向量，用语义匹配代替关键词匹配——你描述内容，它来匹配。

### 核心能力

| 功能 | 说明 |
|------|------|
| **文件管理** | 上传、秒传、分片上传、文件夹、分享、删除，标准云盘功能 |
| **AI 语义搜索** | 上传的文档自动提取内容 → 智能分块 → 向量化，支持自然语言搜索 |
| **多格式解析** | 支持 PDF、Word、Excel、Markdown、代码等 7 种文件格式的内容提取 |
| **AI 文件助手** | 对话式交互，可以搜索文件、查看详情、分享、删除，危险操作需确认 |
| **图床分享** | 图片生成 8 位提取码，方便外部分享 |

### 一个典型的使用流程

```
上传 "2025年度技术方案.docx"
  → 系统自动提取内容，按章节分块
  → 逐块生成 768 维向量，存入 FAISS 索引

三个月后，你输入: "帮我看一下那个关于微服务架构的方案"
  → AI 理解语义，改写查询，FAISS 搜索匹配
  → 同时关键词搜索补充精确匹配结果
  → 双路融合 + LLM 重排序
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
| 文档解析 | PyMuPDF (PDF) + openpyxl (Excel) + python-docx |

## 功能说明

### 文件管理

- **上传与秒传**：上传前先计算文件 MD5 发送给服务端，若目标文件已存在则直接引用，无需重复传输
- **分片上传**：大文件自动拆分为分片，支持断点续传——网络中断后重传只需补传缺失分片
- **文件列表**：按上传时间降序展示，支持分页
- **文件下载**：MinIO 直链下载，不经过后端，不占用 Python 进程资源
- **删除保护**：同一文件被多个用户引用时，仅删除自己的引用，物理文件在最后一个引用者删除时清理

### 分享与图床

- **共享广场**：文件分享后出现在共享广场，其他用户可转存到自己的存储空间
- **下载排行**：Redis ZSET 记录下载量，支持排行榜展示
- **图床模式**：图片生成 8 位随机提取码，方便在论坛、文档中引用
- **取消分享**：分享者可以随时撤销，同时清理 Redis 和共享列表

### AI 文件理解

- **多模态描述**：图片上传后调用 Qwen-VL 生成中文描述，文本文件直接提取内容
- **多格式解析**：PDF（逐页提取+字号检测）、Excel（按工作表+行解析）、Word（解压 XML 提取）、Markdown（标题识别）、代码（函数/类声明识别）
- **自适应分块**：按文件结构切分——Markdown 按 `##` 标题、代码按函数声明、PDF 按页面、Excel 按工作表，每个切片保留上下文标签
- **两级缓存**：`file_ai_desc` 全局缓存（跨用户复用）+ `user_file_ai_desc` 用户记录，同一文件只需一次 AI 调用

### AI 语义搜索

- **自然语言查询**：描述文件内容而非记忆文件名，如"帮我找一下上个月的技术方案"
- **查询智能改写**：模糊查询自动生成 3 个同义变体，取均值向量提高召回率
- **双路混合召回**：向量语义匹配 + 关键词精确匹配，RRF 融合排序
- **LLM 重排序**：Top-20 候选经大模型精排至 Top-10，提升首位命中率
- **结果溯源**：返回 match_context 标注命中位置（如"第3章 微服务架构"）

### AI 文件助手

- **对话式交互**：SSE 实时流式输出，支持多轮对话记忆（Redis，1 小时有效期）
- **7 个工具函数**：search_files、get_file_info、list_recent_files、get_storage_stats、describe_file、delete_file、share_file
- **三级安全模型**：LLM 自主判断（提示词引导）→ 工具级门控（暂停+确认弹窗）→ 服务端鉴权（校验文件归属）
- **危险操作确认**：删除/分享操作必须用户在前端弹窗中确认，5 分钟超时自动取消

### 索引维护

- **脏标记自动重建**：文件删除时标记 dirty，下次搜索前自动从 MySQL 全量重建 FAISS 索引
- **文件锁并发控制**：flock 排他锁，防止多个搜索请求同时触发重建
- **维度自动适配**：切换 embedding 模型时检测到索引维度变化，自动删除旧索引重建

## RAG 检索系统

### 摄入层

- **多格式文本提取**：支持 PDF、Excel、Word、Markdown、代码等 7 类文件的内容提取
- **自适应分块**：按文件结构（章节标题、工作表、函数声明、页面分组）智能切分，每个切片保留上下文标签（如"第三章 核心方案"）
- **逐块向量化**：每个切片独立调用 embedding API 生成 768 维向量，存入 FAISS 索引

### 检索层

- **查询预处理**：结构特征检测 + LLM 分类（模糊/精确）→ 模糊查询自动改写为 3 个同义变体取均值向量
- **双路混合召回**：向量语义搜索（FAISS） + 关键词精确匹配（MySQL LIKE），RRF 融合排序
- **LLM 重排序**：Top-20 候选经大模型精排至 Top-10

### 维护与一致性

- **脏标记自动重建**：文件删除后标记 dirty，下次搜索前自动从 MySQL 全量重建 FAISS 索引
- **文件锁并发控制**：防止多个搜索请求同时触发重建
- **定期巡检函数**：对比 MySQL 记录数与 FAISS ntotal 的一致性

## 快速开始

### 环境要求

- Docker Desktop 20.10+
- Node.js 18+ (前端开发)
- 阿里百炼 DashScope API Key

### 一键启动

```bash
# 1. 克隆项目
git clone <repo-url> && cd AI_YunCunChu-main

# 2. 构建前端
cd frontend && npm install && npm run build && cd ..

# 3. 配置环境变量
cp docker/.env.example docker/.env  # 编辑修改密码

# 4. 启动所有服务
cd docker && docker compose up -d --build

# 5. 将前端构建产物推入 Nginx 容器
docker cp ../frontend/build/. tc_nginx:/app/front/

# 6. 访问
open https://localhost
```

### 首次使用

1. 打开 `https://localhost`（自签名证书需手动信任）
2. 注册账号并登录
3. 在首页输入 DashScope API Key 并保存
4. 上传文件后可使用 AI 搜索和 AI 助手

## 项目结构

```
AI_YunCunChu-main/
├── backend/                        # Python 后端
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── config.py               # 配置管理（支持环境变量）
│   │   ├── dependencies.py         # Token 验证
│   │   ├── database.py             # MySQL 异步连接池
│   │   ├── redis_client.py         # Redis 客户端
│   │   ├── models.py               # SQLAlchemy ORM (7 张表)
│   │   ├── minio_client.py         # MinIO 对象存储客户端
│   │   ├── dashscope_client.py     # DashScope API 封装（原生+兼容端点）
│   │   ├── faiss_service.py        # FAISS 向量索引 + 脏标记 + 文件锁
│   │   ├── chunker.py              # 7 类文件自适应分块器
│   │   ├── agent_tools.py          # Agent 7 个工具函数
│   │   └── routers/
│   │       ├── auth.py             # 注册/登录（加盐 MD5）
│   │       ├── files.py            # 上传/秒传/文件列表
│   │       ├── chunk.py            # 大文件分片上传
│   │       ├── share.py            # 分享/删除/图床
│   │       ├── ai.py               # AI 搜索/描述/重建（含分块+Rerank）
│   │       └── agent.py            # Agent SSE 对话 + 确认门控
│   ├── gunicorn.conf.py            # 生产配置
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                       # React 前端
│   └── src/
│       ├── components/             # NavBar / ChatPanel / ConfirmModal
│       ├── pages/                  # Login / Home / FileList / ImageList ...
│       ├── services/               # auth.js / images.js / ai.js / agent.js
│       └── config/index.js         # API 端点配置
├── docker/
│   ├── docker-compose.yaml         # 5 容器编排
│   ├── .env                        # 密码配置（不入 Git）
│   ├── nginx/                      # Nginx 配置 + Let's Encrypt + SSL
│   └── mysql/                      # 建表 SQL + 迁移脚本
├── PROJECT_GUIDE.md                # 项目架构分析
├── RAG_AGENT_DESIGN.md             # Agent 方案设计
├── RAG_UPGRADE_DESIGN.md           # RAG 检索升级设计
├── IMPLEMENTATION_ROADMAP.md       # 实现思路
├── IMPLEMENTATION_GUIDE.md         # 落地指南
├── DEPLOY_FEASIBILITY.md           # 部署可行性分析
└── PROGRESS.md                     # 实现进度跟踪
```

## 容器架构

```
浏览器 HTTPS → Nginx (443)
                  ├── /             → 前端静态资源
                  ├── /api/         → FastAPI 后端 (8000)
                  ├── /api/agent/   → Agent SSE (8000)
                  └── /files/       → MinIO (9000)

tc_mysql   :3306     MySQL 8.0
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
| `/api/sharepic` | POST | 图床分享 (8 位提取码) |
| `/api/chunk_init` | POST | 分片上传初始化 |
| `/api/chunk_upload` | POST | 分片上传 |
| `/api/chunk_merge` | POST | 分片合并 |
| `/api/ai/search` | POST | AI 语义搜索（双路召回+Rerank） |
| `/api/ai/describe` | POST | AI 文件描述生成（自适应分块） |
| `/api/ai/rebuild` | POST | 重建 FAISS 索引 |
| `/api/agent/chat` | POST | Agent SSE 对话 |
| `/api/agent/confirm` | POST | 确认危险操作 |

## 配置说明

核心配置文件 `backend/app/config.py`，支持环境变量覆盖：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `embedding_model` | tongyi-embedding-vision-flash | 向量化模型 |
| `vl_model` | qwen3.5-omni-flash | 视觉理解模型 |
| `chat_model` | qwen3.5-flash | Agent 对话模型 |
| `embedding_dimension` | 768 | 向量维度 |
| `embedding_max_chars` | 3000 | 单切片最大字符数（分块阈值） |
| `pdf_max_extract_chars` | 8000 | PDF 单次提取最大字符数 |
| `public_server_ip` | (空) | 公网 IP（图片描述需要 DashScope 能访问） |
| `conversation_ttl_seconds` | 3600 | 对话记忆过期时间 |

## 部署上线

1. 购买云服务器（建议 4 核 8 GB）
2. 安装 Docker Desktop
3. 将 `public_server_ip` 设为服务器公网 IP
4. 执行 `docker/nginx/setup-certbot.sh 你的域名` 获取 SSL 证书
5. `docker compose up -d --build`
