# 实现思路

## 整体策略

**自下而上，逐层验证。** 每一层写完后独立测试通过，再往上叠下一层。绝不一口气写完再调试。

---

## 八层实现顺序

### 第一层：基础设施搭建
新建 `docker/`（覆盖旧版），写 `docker-compose.yaml` + Nginx 配置 + MySQL init.sql，5 个容器全部 `docker compose up` 健康运行。不写一行业务代码。

### 第二层：数据模型
SQLAlchemy 映射 7 张表。不写接口，只写模型 + 连接池，用测试脚本验证增删改查。

### 第三层：认证
`/api/reg` + `/api/login`。先让用户能注册和登录，Token 存入 Redis。写一个 `check_token()` 工具函数，后续所有接口复用。

### 第四层：核心上传下载
`/api/upload`(MinIO) + `/api/md5`(秒传) + `/api/myfiles`(列表) + 分片上传三件套。这一步完后系统"能用"——文件能进能出。

### 第五层：文件管理
`/api/dealfile`(分享/删除/PV) + `/api/sharefiles` + `/api/dealsharefile` + `/api/sharepic`。全部 CRUD 闭环。

### 第六层：AI 能力
DashScope 封装 + FAISS 封装 + `/api/ai`(describe/search/rebuild)。语义搜索跑通。

### 第七层：Agent
7 个工具函数（底层调前六层写好的 service）+ ReAct 循环 + SSE + 确认机制。对话式文件操作。

### 第八层：前端适配 + 加固
`picture_bed/` 搬入 `frontend/`，加 ChatPanel。Gunicorn 生产配置，安全加固。

---

## 每层的执行模式

```
1. 创建/修改文件
2. docker compose restart <service>
3. curl 或浏览器验证
4. 验证通过 → 更新 PROGRESS.md → 进入下一层
   验证失败 → 修 → 再验证
```

## 关键设计决策（不再讨论，直接执行）

| 问题 | 决策 |
|------|------|
| 后端框架 | FastAPI + Gunicorn |
| ORM | SQLAlchemy async |
| 数据库驱动 | aiomysql |
| 文件存储 | MinIO + minio-py SDK |
| AI SDK | OpenAI SDK 指向 DashScope 兼容端点 |
| 向量检索 | faiss-cpu (pip)，与 C FAISS 索引二进制兼容 |
| Agent 方案 | ReAct + DashScope function calling + SSE |
| Token | `secrets.token_hex(16)` 替代 DES+Base64+MD5 |
| 密码 | `MD5(salt + MD5(password))` 不变，保持前端兼容 |
| Python 默认版本 | 3.12.2 (已确认) |
| pip 源 | 清华源 (已配置) |
| Docker 镜像加速 | docker.1ms.run (已配置) |
