# RAG 系统升级 —— 实现进度追踪

> 状态图例：⬜ 未开始 | 🔵 进行中 | ✅ 已完成 | ❌ 阻塞

---

## 阶段一：分块架构

| 项 | 文件 | 状态 |
|----|------|------|
| 通用段落切分器 FallbackChunker | `backend/app/chunker.py` | ✅ |
| Markdown 标题切分器 | `backend/app/chunker.py` | ✅ |
| 代码函数/类切分器 | `backend/app/chunker.py` | ✅ |
| PDF 页面 + 字号检测切分器 | `backend/app/chunker.py` | ✅ |
| Excel 工作表切分器 | `backend/app/chunker.py` | ✅ |
| docx 标题样式切分器 | `backend/app/chunker.py` | ✅ |
| 分块策略路由（按文件类型选择切分器） | `backend/app/chunker.py` | ✅ |
| 上下文继承逻辑（超限切片带父标题） | `backend/app/chunker.py` | ✅ |
| `ai.py` 描述生成链路接入分块 | `backend/app/routers/ai.py` | ✅ |
| 验证：上传长文本文件 → 分块数 > 1，chunk 语义完整 | — | ✅ |

---

## 阶段二：数据模型升级

| 项 | 文件 | 状态 |
|----|------|------|
| `user_file_ai_desc` 添加 `chunk_index` 字段（默认 0） | `backend/app/models.py` | ✅ |
| `user_file_ai_desc` 添加 `context_label` 字段 | `backend/app/models.py` | ✅ |
| `(user, md5, chunk_index)` 联合唯一约束 | `backend/app/models.py` | ✅ |
| `file_ai_desc` 按 `(md5, chunk_index)` 调整全局缓存结构 | `backend/app/models.py` | ✅ |
| 数据库 DDL 迁移脚本 | `docker/mysql/migration_chunk.sql` | ✅ |
| 写入链路适配 chunk_index（INSERT 时带序号） | `backend/app/routers/ai.py` | ✅ |
| 全局缓存复制逻辑适配（copy_cache → 按 md5 复制所有 chunk） | `backend/app/routers/ai.py` | ✅ |
| FAISS add_vector 适配（每个 chunk 独立 faiss_id） | `backend/app/faiss_service.py` | ✅ |
| 验证：上传长文件 → MySQL 中 chunk_index 正确递增 | — | ✅ |

---

## 阶段三：检索升级 —— 查询改写 + 混合召回

| 项 | 文件 | 状态 |
|----|------|------|
| 查询结构特征检测（扩展名/日期/编号/英文词匹配） | `backend/app/routers/ai.py` | ⬜ |
| LLM 查询分类（模糊 vs 精确，约 10 token） | `backend/app/routers/ai.py` | ⬜ |
| LLM 查询改写（模糊查询 → 生成 3 个变体 → 均值向量） | `backend/app/routers/ai.py` | ⬜ |
| 关键词提取函数（正则匹配精确锚点） | `backend/app/routers/ai.py` | ⬜ |
| 关键词路门控（无精确词则跳过） | `backend/app/routers/ai.py` | ⬜ |
| MySQL 关键词搜索（多级排序：命中数 > 完全匹配 > 时间） | `backend/app/routers/ai.py` | ⬜ |
| RRF 双路排名融合 | `backend/app/routers/ai.py` | ⬜ |
| FAISS 搜索适配 chunk（回查时读取 context_label） | `backend/app/routers/ai.py` | ⬜ |
| 按 md5 去重合并（同文件多 chunk 命中取最高分） | `backend/app/routers/ai.py` | ⬜ |
| 结果增加 `match_context` 和 `source` 字段 | `backend/app/routers/ai.py` | ⬜ |
| 验证：模糊查询 / 精确查询 / 混合查询三种场景结果正确 | — | ⬜ |

---

## 阶段四：LLM 重排序

| 项 | 文件 | 状态 |
|----|------|------|
| 重排序 prompt 设计（候选列表 + query → 相关性排序） | `backend/app/routers/ai.py` | ⬜ |
| LLM 重排序调用（Top-20 → Top-10） | `backend/app/routers/ai.py` | ⬜ |
| 重排序失败的降级策略（回退到 RRF 排名） | `backend/app/routers/ai.py` | ⬜ |
| 验证：A/B 对比重排序前后 Top-5 准确率 | — | ⬜ |

---

## 阶段五：索引维护完善

| 项 | 文件 | 状态 |
|----|------|------|
| `is_dirty(user)` 检测函数 | `backend/app/faiss_service.py` | ⬜ |
| `mark_dirty(user)` 标记函数 | `backend/app/faiss_service.py` | ⬜ |
| `clear_dirty(user)` 清除函数 | `backend/app/faiss_service.py` | ⬜ |
| 搜索前自动 rebuild（检测 dirty → 加锁 → 重建 → 清除） | `backend/app/routers/ai.py` | ⬜ |
| 文件锁（防止并发同时重建） | `backend/app/faiss_service.py` | ⬜ |
| `rebuild_from_db` 适配分块（重建时按 chunk_index 回写） | `backend/app/faiss_service.py` | ⬜ |
| `share.py` 删除接入 mark_dirty（替代旧标记逻辑） | `backend/app/routers/share.py` | ⬜ |
| 定期巡检定时任务（对比 MySQL 切片数与 FAISS ntotal） | `backend/app/faiss_service.py` | ⬜ |
| 验证：删除文件 → 下次搜索自动重建 → 已删文件不可搜到 | — | ⬜ |

---

## 阶段六：Agent 展示优化

| 项 | 文件 | 状态 |
|----|------|------|
| `search_files` 工具结果压缩（只传前两句描述 + match_context） | `backend/app/agent_tools.py` | ⬜ |
| 搜索结果附带文件 URL（引用溯源） | `backend/app/agent_tools.py` | ⬜ |
| Agent prompt 增加"引用信息时注明来源文件"规则 | `backend/app/routers/agent.py` | ⬜ |
| 追问展开逻辑（用户追问时拉取全切片） | `backend/app/routers/agent.py` | ⬜ |
| 验证：Agent 对话中搜索结果带来源链接，追问可展开 | — | ⬜ |

---

## 最后更新

_每完成一项，更新状态并记录日期。_
