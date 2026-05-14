# 实现进度跟踪

> 状态图例：⬜ 未开始 | 🔵 进行中 | ✅ 已完成 | ❌ 阻塞

---

## 第一层：基础设施搭建

| 项 | 文件 | 状态 |
|----|------|------|
| 新建 docker-compose.yaml | `docker/docker-compose.yaml` | ✅ |
| 新建 .env 环境变量 | `docker/.env` | ✅ |
| 新建 nginx Dockerfile | `docker/nginx/Dockerfile` | ✅ |
| 新建 nginx.conf | `docker/nginx/nginx.conf` | ✅ |
| 新建 mysql init.sql（复用旧版） | `docker/mysql/init.sql` | ✅ |
| 新建后端 Dockerfile | `backend/Dockerfile` | ✅ |
| 新建后端 requirements.txt | `backend/requirements.txt` | ✅ |
| 新建后端 config.py | `backend/app/config.py` | ✅ |
| 新建后端 main.py（空壳 + health 端点） | `backend/app/main.py` | ✅ |
| 生成 SSL 自签名证书 | `docker/nginx/ssl/` | ✅ |
| docker compose up 全容器健康 | — | ✅ |

---

## 第二层：数据模型

| 项 | 文件 | 状态 |
|----|------|------|
| 数据库连接引擎 | `backend/app/database.py` | ✅ |
| Redis 客户端 | `backend/app/redis_client.py` | ✅ |
| ORM 模型定义（7 张表） | `backend/app/models.py` | ✅ |
| 验证：增删改查测试通过 | — | ✅ |

---

## 第三层：认证

| 项 | 文件 | 状态 |
|----|------|------|
| 注册接口 `/api/reg` | `backend/app/routers/auth.py` | ✅ |
| 登录接口 `/api/login` | `backend/app/routers/auth.py` | ✅ |
| Token 工具函数 `check_token()` | `backend/app/dependencies.py` | ✅ |
| 验证：注册 → 登录 → 拿 Token → 其他接口返回 200 | — | ✅ |

---

## 第四层：核心上传下载

| 项 | 文件 | 状态 |
|----|------|------|
| MinIO 客户端封装 | `backend/app/minio_client.py` | ✅ |
| 秒传检测 `/api/md5` | `backend/app/routers/files.py` | ✅ |
| 普通上传 `/api/upload` | `backend/app/routers/files.py` | ✅ |
| 文件列表 `/api/myfiles` | `backend/app/routers/files.py` | ✅ |
| 分片初始化 `/api/chunk_init` | `backend/app/routers/chunk.py` | ✅ |
| 分片上传 `/api/chunk_upload` | `backend/app/routers/chunk.py` | ✅ |
| 分片合并 `/api/chunk_merge` | `backend/app/routers/chunk.py` | ✅ |
| 验证：上传 → 列表可见 → 下载正常 → 秒传生效 | — | ✅ |

---

## 第五层：文件管理

| 项 | 文件 | 状态 |
|----|------|------|
| 分享/删除/PV `/api/dealfile` | `backend/app/routers/share.py` | ✅ |
| 共享广场 `/api/sharefiles` | `backend/app/routers/share.py` | ✅ |
| 转存/取消分享 `/api/dealsharefile` | `backend/app/routers/share.py` | ✅ |
| 图床分享 `/api/sharepic` | `backend/app/routers/share.py` | ✅ |
| 验证：分享→广场可见→转存→删除→引用计数正确 | — | ✅ |

---

## 第六层：AI 能力

| 项 | 文件 | 状态 |
|----|------|------|
| DashScope API 封装 | `backend/app/dashscope_client.py` | ✅ |
| FAISS 向量索引封装 | `backend/app/faiss_service.py` | ✅ |
| AI 搜索 `/api/ai/search` | `backend/app/routers/ai.py` | ⬜ |
| AI 描述生成 `/api/ai/describe` | `backend/app/routers/ai.py` | ⬜ |
| 索引重建 `/api/ai/rebuild` | `backend/app/routers/ai.py` | ⬜ |
| 验证：搜索返回结果带 score + description | — | ⬜ |

---

## 第七层：Agent

| 项 | 文件 | 状态 |
|----|------|------|
| 7 个工具函数实现 | `backend/app/agent_tools.py` | ⬜ |
| Agent 对话接口（SSE）`/api/agent/chat` | `backend/app/routers/agent.py` | ⬜ |
| 确认接口 `/api/agent/confirm` | `backend/app/routers/agent.py` | ⬜ |
| 对话记忆（Redis） | `backend/app/agent_tools.py` 集成 | ⬜ |
| 危险操作确认机制 | `backend/app/routers/agent.py` | ⬜ |
| 验证：对话→搜索→分享确认→执行成功 | — | ⬜ |

---

## 第八层：前端适配 + 加固

| 项 | 文件 | 状态 |
|----|------|------|
| 前端项目搭建 | `frontend/` | ⬜ |
| ChatPanel 聊天面板 | `frontend/src/components/ChatPanel.js` | ⬜ |
| ConfirmModal 确认弹窗 | `frontend/src/components/ConfirmModal.js` | ⬜ |
| SSE 客户端 | `frontend/src/services/agent.js` | ⬜ |
| NavBar 聊天入口 | `frontend/src/components/NavBar.js` | ⬜ |
| Gunicorn 生产配置 | `backend/gunicorn.conf.py` | ⬜ |
| Let's Encrypt 证书 | `docker/nginx/ssl/` | ⬜ |
| 最终全链路验证 | — | ⬜ |

---

## 最后一次更新

_每完成一项，更新状态并记录日期。_
