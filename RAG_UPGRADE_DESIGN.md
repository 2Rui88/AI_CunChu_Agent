# RAG 系统升级设计方案

## 一、概述

将当前"单文件单向量 + 固定截断 + 纯向量搜索"升级为"自适应分块 + 双路混合召回 + LLM 重排序"的 RAG 系统。核心目标：长文档的语义信息不再被截断丢失，搜索从"单一向量匹配"升级为"多机制融合排序"。

---

## 二、摄入：多模态描述 + 自适应分块

### 2.1 描述生成

文件上传后按类型分流提取语义内容：

| 文件类型 | 提取方式 |
|----------|---------|
| 图片 | Qwen-VL 多模态生成中文描述 |
| PDF | PyMuPDF 逐页提取文本，空页跳过 |
| Excel | openpyxl 逐行提取，表头随数据行输出 |
| docx | 解压 ZIP 提取 `<w:t>` 文本节点 |
| 文本/代码 | 直接读取内容 |
| 其他 | 文件名 + 类型标签 |

### 2.2 分块判断

```
描述文本长度 ≤ 3000 字符 → 不分块，chunk_index = 0
描述文本长度 > 3000 字符 → 按文件类型选择切分策略
```

### 2.3 分块策略

| 文件类型 | 切分边界 | 示例 |
|----------|---------|------|
| Markdown | `##` 标题段落 | 每个标题段落一个 chunk |
| 代码 | 函数/类声明 | 每个函数或类一个 chunk |
| PDF | 页面 + 字号突变检测 | 页面分组一个 chunk |
| Excel | 工作表 + 连续数据行 | 表头行 + N 行数据一个 chunk |
| docx | 标题样式 + 段落 | 每个章节一个 chunk |
| 纯文本 | 段落（连续空行） | 每个段落组一个 chunk |

### 2.4 上下文继承

超限切片带上父标题，保证每个 chunk 语义独立完整。

```
"第三章 核心方案" （4000 字，超限切为 2 个 chunk）

chunk_0: context_label = "第三章 核心方案"
         text = "第三章 核心方案\n段落1...段落5..."

chunk_1: context_label = "第三章 核心方案"
         text = "第三章 核心方案\n段落6...段落10..."
```

### 2.5 分块器架构

采用策略模式，所有切分器实现统一接口：

```python
def chunk_text(text: str, file_type: str, metadata: dict, max_chars: int) -> list[Chunk]:
    """
    返回: [(chunk_index, chunk_text, context_label), ...]
    """
```

新增文件 `backend/app/chunker.py`，包含 `FallbackChunker`（段落切分兜底）、`PDFChunker`、`CodeChunker` 等策略类，按 `file_type` 路由选择。

---

## 三、存储：MySQL 真相源 + FAISS 搜索层

### 3.1 数据模型

`user_file_ai_desc` 每条记录对应一个切片：

| 字段 | 说明 |
|------|------|
| `id` | 自增主键 |
| `user` | 用户标识 |
| `md5` | 文件标识 |
| `chunk_index` | 切片序号（0, 1, 2...） |
| `description` | 切片文本 |
| `embedding` | 768 维向量（float32 二进制） |
| `faiss_id` | FAISS 索引中的位置 |
| `context_label` | 展示标注（"第三章 核心方案"） |
| `status` | 1=完成 |

唯一约束：`(user, md5, chunk_index)`

短文件 `chunk_index` 恒为 0，共用同一套存储和搜索链路。

### 3.2 写入流程

```
分块完成
  │
  ├─→ 每个切片调用 embedding API → 768 维向量
  │
  ├─→ MySQL: INSERT INTO user_file_ai_desc
  │     (user, md5, chunk_index, description, embedding, context_label)
  │
  └─→ FAISS: add_vector(user, vec)
        ├─ L2 归一化 → idx.add(vec) → 获得 faiss_id
        ├─ save_index(user) → 持久化到磁盘
        └─ 回写 MySQL: UPDATE faiss_id
```

### 3.3 全局缓存

`file_ai_desc` 表按 md5 去重，同一文件跨用户只需一次 AI 调用。分块模式下全局缓存以 `(md5, chunk_index)` 为单位存储。

---

## 四、检索：四阶段搜索链路

### 4.1 一次 LLM 查询规划（类型判断 + 改写 + 关键词生成）

查询类型与两路召回参数由**一次 LLM 调用**统一给出（原先"分类一次 + 改写一次"
的两次调用合并为一次，且程序侧不再用正则做任何类型判断）：

```
用户输入 query
  │
  └─ 一次 LLM 调用 plan_search_query，返回固定 JSON：
     {
       "query_type": "fuzzy | exact",
       "vector_queries": ["..."],   ← 向量路输入（模糊=3 个同义改写；精确=1 个）
       "mysql_keywords": ["..."]    ← 关键词路输入（精确=文件名锚点词；模糊通常为空）
     }
        
     例（模糊）: "上周的会议记录"
     → {"query_type": "fuzzy",
        "vector_queries": ["会议纪要 最近一周", "上周会议记录", "会议记录 上周"],
        "mysql_keywords": []}
```

程序只做结构校验：JSON 合法性、query_type 枚举、数组与字符串长度上限；
非法/网络失败 → 安全降级（原始查询单条向量召回）。每个 vector_query 分别
embedding 后取均值向量（向量合并逻辑不变）。

### 4.2 双路混合召回（带门控）

**路 A — 向量路（始终触发）**：

```
综合向量 → FAISS search(user, vec, top_k=20)
→ 按 faiss_id 回查 MySQL 获取切片信息
→ [{md5, chunk_index, context_label, cosine_score, rank_向量}, ...]
```

**路 B — 关键词路（由查询规划的 mysql_keywords 驱动）**：

```
1. 关键词来源：4.1 规划返回的 mysql_keywords
   （模糊查询通常为空——"上周的会议报告"只能提取到"会议""报告"这类
    高频通用词，LIKE 噪声大，LLM 规划时就不生成）

2. 门控判断：
   - mysql_keywords 为空 → 跳过关键词路，纯向量路返回
   - 非空 → 启动关键词路

3. MySQL 搜索：
   SELECT FROM user_file_list
   WHERE user=? AND (file_name LIKE '%词1%' OR ...)
   ORDER BY 命中关键词数量 DESC, 完全匹配优先
   LIMIT 20
```

另有一条可用性兜底（与类型判断无关）：向量索引为空（新用户尚未建立任何
AI 描述）时关键词路是唯一可用路径，此时用原始查询兜底跑一次 LIKE，
保证按文件名精确搜索不至于返回"没有找到"。

**统一 RRF 融合**（两路召回统一合并为一张候选文件表，不再各自输出一套排序）：

```
以 md5 为唯一候选键合并：
  {md5, filename, vector_rank(可空), keyword_rank(可空), rrf_score, match_context}

RRF_score = 1/(60 + vector_rank) + 1/(60 + keyword_rank)

只有向量路结果:   RRF = 1/(60 + vector_rank)
只有关键词路结果: RRF = 1/(60 + keyword_rank)
两路都出现:       RRF = 1/(60 + vector_rank) + 1/(60 + keyword_rank)

按 RRF 降序 → 决定是否重排
```

source 字段（向量路/关键词路/双路混合）仅用于调试展示，不参与排序；
两路不设各自优先级，也不做"先排向量再插入关键词"。

RRF 不需要调权重。两路分数量纲不同（余弦相似度 vs. LIKE 命中），用排名融合天然规避量纲问题。一个文件如果在两路都排名靠前，RRF 自动加权；只在一路出现则自动降权。常数 60 抑制排名靠后的噪声。

### 4.3 LLM 重排序（候选数 >5 时才执行）

余弦相似度和 RRF 只看向量距离和字符串匹配，不区分哪个候选真正"回答用户的问题"。LLM 做最终精排。

```
统一 RRF 排序完成后的候选文件数
  │
  ├─ 候选数 ≤5 → 直接按 RRF 顺序返回（跳过 LLM，避免一两个候选也付一次秒级调用）
  │
  └─ 候选数 >5 → 取前 20 个送 LLM 精排
        输入：文件名 + 描述 + 匹配上下文 + RRF 分数 + 查询原文
        ├─ 重排成功 → 使用重排顺序（Top-10）
        └─ 重排失败 → 回退统一 RRF 顺序
```

只做排序不生成文本，token 成本约为一次 embedding 的一半。

### 4.4 去重与组装

同文件多个切片命中 → 按 md5 去重，保留最高分切片。

```
chunk_1 (第4-6章 核心方案) score=0.92
chunk_0 (第1-3章 背景介绍) score=0.65  ← 同 md5，丢弃

最终返回:
  [
    {md5, filename, description, url, score=0.92,
     match_context: "第4-6章 核心方案",
     source: "向量路"},
    ...
  ]
```

`match_context` 告知用户搜索命中文件的具体位置，`source` 标注结果来源便于后续效果分析。

---

## 五、删除与索引维护

### 5.1 删除流程

```
用户删除文件
  ├─ MySQL: DELETE FROM user_file_ai_desc WHERE user=? AND md5=?
  │        （chunk_index 0~N 全部删除）
  └─ 标记 dirty: /tmp/faiss_locks/<md5_user>.dirty
```

删除不需要知道 chunk_index，user + md5 即覆盖所有切片。

### 5.2 搜索时自动重建

```
搜索请求到达
  ├─ 检测 is_dirty(user)
  │     ├─ True → 加文件锁 → 从 MySQL 全量加载 user 所有向量
  │     │         → rebuild_from_db(user, vectors)
  │     │         → 回写所有切片的 faiss_id
  │     │         → 清除 dirty → 释放锁
  │     └─ False → 跳过
  └─ 正常搜索
```

文件锁保证并发搜索不会同时触发多次重建。

### 5.3 定期巡检

```
定时任务（每天）:
  对比 COUNT(user_file_ai_desc WHERE user=?) 与 FAISS ntotal
  不一致 → 自动重建该用户索引
```

---

## 六、Agent 展示优化

| 环节 | 做法 |
|------|------|
| 结果压缩 | 只传"文件名 + 前两句描述 + match_context"，不传完整列表 |
| 引用溯源 | 结果附带文件 URL，Agent 关键结论标注来源 |
| 追问展开 | 用户追问时拉取文件全部切片，展开完整描述 |

---

## 七、实施顺序

| 阶段 | 内容 | 改动范围 |
|:---:|------|------|
| 一 | 分块架构：段落切分兜底 + 各文件类型策略 | 新 `chunker.py` + 修改 `ai.py` |
| 二 | 模型加 `chunk_index` + `context_label` 字段 | `models.py` + 数据库迁移 |
| 三 | 查询改写 + 关键词门控 + RRF 融合 | `ai.py` 搜索入口 |
| 四 | LLM 重排序 | `ai.py` 搜索后处理 |
| 五 | dirty 自动检测闭环 + 定期巡检 | `ai.py` + `faiss_service.py` |
| 六 | Agent 结果压缩 + 引用溯源 | `agent.py` + `agent_tools.py` |

每阶段独立上线，后一阶段不依赖前一阶段。
