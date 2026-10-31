"""
搜索链路回归测试 —— 一次 LLM 查询规划 / 统一 RRF 合并 / 重排门槛

覆盖场景（对应改造方案任务 7）：
1.  模糊查询返回改写查询            （_parse_plan / plan_search_query）
2.  精确查询返回 MySQL 关键词        （_parse_plan / plan_search_query）
3.  LLM 返回非法 JSON               （_parse_plan / plan_search_query 安全降级）
4.  只有向量路结果                   （_merge_rrf 单路 RRF）
5.  只有关键词路结果                 （_merge_rrf 单路 RRF）
6.  两路都有结果                     （_merge_rrf 相加公式）
7.  两路排名不一致                   （_merge_rrf 按统一分数排序）
8.  候选数为 5                       （跳过 LLM 重排）
9.  候选数为 6                       （触发 LLM 重排）
10. LLM 重排失败                     （回退统一 RRF 顺序）
11. 查询结果为空                     （count=0，不重排）
另覆盖：结构约束（数量/长度/去重）、向量路输入缺失兜底、
        索引为空时关键词路兜底、关键词路由 LLM 规划驱动。
"""
import json
import types

import numpy as np
import pytest

from app.routers import ai


# ── 测试辅助 ──

def _vec_entry(md5: str, rank: int, score: float = 0.9) -> dict:
    """构造 _vector_lookup 输出格式的向量路候选"""
    return {
        "md5": md5, "filename": f"file_{md5}.mdx", "description": f"desc {md5}",
        "url": "http://x/u", "size": "1", "type": "md",
        "score": score, "vector_rank": rank,
        "match_context": f"章节{rank}", "chunk_index": 0,
    }


def _kw_entry(md5: str, filename: str = None) -> dict:
    """构造 _keyword_search 输出格式的关键词路候选"""
    return {
        "md5": md5, "filename": filename or f"file_{md5}.mdx", "url": "http://x/u",
        "size": "1", "type": "md", "pv": 0, "hit_count": 1, "source": "keyword",
    }


def _fake_llm_client(content: str):
    """返回 chat.completions.create 固定输出 content 的伪 OpenAI 客户端"""
    message = types.SimpleNamespace(content=content)
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    async def create(**kw):
        return resp

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )


# ═══════════════════════════════════════════════════════════
#  1/2/3. 查询规划：解析 / 校验 / 降级
# ═══════════════════════════════════════════════════════════

class TestPlanParse:
    def test_fuzzy_plan_with_rewrites(self):
        """场景 1：模糊查询返回多个改写查询，关键词为空"""
        raw = json.dumps({
            "query_type": "fuzzy",
            "vector_queries": ["云存储技术演进", "对象存储体系", "分布式存储技术"],
            "mysql_keywords": [],
        }, ensure_ascii=False)
        plan = ai._parse_plan(raw, "讲讲云存储的发展")
        assert plan["query_type"] == "fuzzy"
        assert plan["vector_queries"] == ["云存储技术演进", "对象存储体系", "分布式存储技术"]
        assert plan["mysql_keywords"] == []

    def test_exact_plan_with_keywords(self):
        """场景 2：精确查询返回 MySQL 关键词"""
        raw = json.dumps({
            "query_type": "exact",
            "vector_queries": ["对象存储技术演进与实践"],
            "mysql_keywords": ["对象存储技术演进与实践"],
        }, ensure_ascii=False)
        plan = ai._parse_plan(raw, "对象存储技术演进与实践.docx")
        assert plan["query_type"] == "exact"
        assert plan["mysql_keywords"] == ["对象存储技术演进与实践"]
        assert len(plan["vector_queries"]) == 1

    @pytest.mark.parametrize("raw", [
        "完全不是 JSON",
        "{unclosed",
        '{"query_type": "unknown_type", "vector_queries": ["a"], "mysql_keywords": []}',
        '{"query_type": "fuzzy", "vector_queries": "not-a-list", "mysql_keywords": []}',
        '["json", "but", "not", "object"]',
        "",
    ])
    def test_invalid_json_falls_back(self, raw):
        """场景 3：非法输出 → 安全降级（原始查询单条向量召回）"""
        plan = ai._parse_plan(raw, "原始查询")
        assert plan == {
            "query_type": "fuzzy",
            "vector_queries": ["原始查询"],
            "mysql_keywords": [],
        }

    def test_json_in_code_fence_parsed(self):
        """LLM 加了 ```json 围栏也能解析（属合法输出而非降级）"""
        raw = '```json\n{"query_type": "exact", "vector_queries": ["q"], "mysql_keywords": ["k"]}\n```'
        plan = ai._parse_plan(raw, "query")
        assert plan["query_type"] == "exact"
        assert plan["mysql_keywords"] == ["k"]

    def test_empty_vector_queries_backfilled_with_original(self):
        """向量路输入缺失 → 原始查询兜底（保证语义召回不丢）"""
        raw = '{"query_type": "fuzzy", "vector_queries": [], "mysql_keywords": ["k"]}'
        plan = ai._parse_plan(raw, "原始查询")
        assert plan["vector_queries"] == ["原始查询"]
        assert plan["mysql_keywords"] == ["k"]

    def test_structural_caps(self):
        """结构约束：条数上限、长度截断、去重、非字符串项剔除"""
        raw = json.dumps({
            "query_type": "fuzzy",
            "vector_queries": [f"查询{i}" for i in range(10)] + ["查询1", 123, "  "],
            "mysql_keywords": ["k" * 200, "kw", None, "kw"],
        }, ensure_ascii=False)
        plan = ai._parse_plan(raw, "q")
        assert len(plan["vector_queries"]) == ai._PLAN_MAX_QUERIES
        assert len(set(plan["vector_queries"])) == len(plan["vector_queries"])
        assert all(isinstance(v, str) and v for v in plan["vector_queries"])
        assert len(plan["mysql_keywords"]) == 2
        assert len(plan["mysql_keywords"][0]) == ai._PLAN_MAX_KEYWORD_CHARS


class TestPlanSearchQuery:
    async def test_llm_success(self, monkeypatch):
        """一次 LLM 调用返回规划（模糊：3 改写 / 空关键词）"""
        monkeypatch.setattr(ai, "create_client",
                            lambda key: _fake_llm_client(json.dumps({
                                "query_type": "fuzzy",
                                "vector_queries": ["a", "b", "c"],
                                "mysql_keywords": [],
                            })))
        plan = await ai.plan_search_query("随便找找", "key")
        assert plan["vector_queries"] == ["a", "b", "c"]

    async def test_llm_network_error_falls_back(self, monkeypatch):
        """场景 3：LLM 网络失败 → 安全降级"""
        def boom(key):
            raise RuntimeError("network down")

        monkeypatch.setattr(ai, "create_client", boom)
        plan = await ai.plan_search_query("查询", "key")
        assert plan["vector_queries"] == ["查询"]
        assert plan["mysql_keywords"] == []

    async def test_llm_invalid_content_falls_back(self, monkeypatch):
        """场景 3：LLM 返回非法 JSON → 安全降级"""
        monkeypatch.setattr(ai, "create_client",
                            lambda key: _fake_llm_client("我觉得这个查询不太好"))
        plan = await ai.plan_search_query("查询", "key")
        assert plan["query_type"] == "fuzzy"
        assert plan["vector_queries"] == ["查询"]


# ═══════════════════════════════════════════════════════════
#  4/5/6/7. 统一合并 + RRF
# ═══════════════════════════════════════════════════════════

class TestMergeRRF:
    K = 60

    def test_vector_only(self):
        """场景 4：只有向量路 → rrf = 1/(60+vector_rank)"""
        vec = {"m1": _vec_entry("m1", 1), "m2": _vec_entry("m2", 2)}
        files = ai._merge_rrf(vec, [])
        assert [f["md5"] for f in files] == ["m1", "m2"]
        assert files[0]["rrf_score"] == round(1 / (self.K + 1), 5)
        assert files[1]["rrf_score"] == round(1 / (self.K + 2), 5)
        assert files[0]["keyword_rank"] is None
        assert files[0]["source"] == "向量路"

    def test_keyword_only(self):
        """场景 5：只有关键词路 → rrf = 1/(60+keyword_rank)"""
        kws = [_kw_entry("k2"), _kw_entry("k1")]
        files = ai._merge_rrf({}, kws)
        assert [f["md5"] for f in files] == ["k2", "k1"]
        assert files[0]["rrf_score"] == round(1 / (self.K + 1), 5)
        assert files[0]["vector_rank"] is None
        assert files[0]["source"] == "关键词路"
        assert files[0]["match_context"] == ""

    def test_both_paths_sum(self):
        """场景 6：两路都有 → 相加公式"""
        vec = {"m1": _vec_entry("m1", 1)}
        kws = [_kw_entry("m1")]
        files = ai._merge_rrf(vec, kws)
        assert len(files) == 1
        assert files[0]["vector_rank"] == 1
        assert files[0]["keyword_rank"] == 1
        assert files[0]["rrf_score"] == round(1 / (self.K + 1) + 1 / (self.K + 1), 5)
        assert files[0]["source"] == "双路混合"

    def test_ranks_disagree_unified_order(self):
        """场景 7：两路排名不一致 → 按统一 rrf_score 排序，不偏向任何一路"""
        # m_a：向量第 1 / 关键词第 4；m_b：向量第 2 / 关键词第 1；m_c：向量第 3 / 无关键词
        vec = {"a": _vec_entry("a", 1), "b": _vec_entry("b", 2), "c": _vec_entry("c", 3)}
        kws = [_kw_entry("d"), _kw_entry("b"), _kw_entry("e"), _kw_entry("a")]
        files = ai._merge_rrf(vec, kws)

        def score(v, kw):
            s = 1 / (self.K + v) if v else 0
            s += 1 / (self.K + kw) if kw else 0
            return round(s, 5)

        expect = {
            "a": (1, 4, score(1, 4)),
            "b": (2, 2, score(2, 2)),
            "c": (3, None, score(3, None)),
            "d": (None, 1, score(None, 1)),
            "e": (None, 3, score(None, 3)),
        }
        assert len(files) == 5
        assert files == sorted(files, key=lambda f: f["rrf_score"], reverse=True)
        for f in files:
            v, kw, s = expect[f["md5"]]
            assert f["vector_rank"] == v
            assert f["keyword_rank"] == kw
            assert f["rrf_score"] == s
        # b（双路均靠前）必须排在 d（仅关键词第 1）之前
        assert [f["md5"] for f in files].index("b") < [f["md5"] for f in files].index("d")

    def test_empty_inputs(self):
        """两路都空 → 空结果"""
        assert ai._merge_rrf({}, []) == []


# ═══════════════════════════════════════════════════════════
#  8/9/10/11. semantic_search 端到端（mock 外部依赖）
# ═══════════════════════════════════════════════════════════

def _patch_pipeline(monkeypatch, *, n_vec=0, n_kw=0, index_ready=True,
                    plan=None, rerank=None):
    """mock 掉 semantic_search 的全部外部依赖，返回各 mock 的调用记录"""
    calls = {"keyword_search": [], "rerank": 0, "rerank_args": None}

    async def fake_plan(query, api_key):
        return plan or {
            "query_type": "fuzzy",
            "vector_queries": ["q1", "q2", "q3"],
            "mysql_keywords": [],
        }

    async def fake_embed(queries, api_key, fallback_query, timing=None):
        return np.zeros(8, dtype=np.float32)

    def fake_faiss(user, vec, top_k):
        # 每个文件一个 faiss_id，分数均过阈值
        return [(i, 0.9 - i * 0.01) for i in range(n_vec)]

    async def fake_keyword_search(user, keywords, query="", limit=20):
        calls["keyword_search"].append(list(keywords))
        return [_kw_entry(f"k{i}") for i in range(n_kw)]

    async def fake_vector_lookup(user, valid):
        return {f"v{i}": _vec_entry(f"v{i}", rank=i + 1) for i in range(len(valid))}

    if rerank is None:
        async def fake_rerank(query, files, api_key):
            calls["rerank"] += 1
            calls["rerank_args"] = (query, files)
            return files  # 保序返回
    else:
        async def fake_rerank(query, files, api_key):
            calls["rerank"] += 1
            calls["rerank_args"] = (query, files)
            return await rerank(query, files, api_key)

    monkeypatch.setattr(ai, "is_dirty", lambda user: False)
    monkeypatch.setattr(ai, "get_ntotal", lambda user: 100 if index_ready else 0)
    monkeypatch.setattr(ai, "plan_search_query", fake_plan)
    monkeypatch.setattr(ai, "_embed_queries", fake_embed)
    monkeypatch.setattr(ai, "faiss_search", fake_faiss)
    monkeypatch.setattr(ai, "_keyword_search", fake_keyword_search)
    monkeypatch.setattr(ai, "_vector_lookup", fake_vector_lookup)
    monkeypatch.setattr(ai, "_rerank_with_llm", fake_rerank)
    return calls


class TestSemanticSearchFlow:
    async def test_five_candidates_skip_rerank(self, monkeypatch):
        """场景 8：候选数 =5 → 不调 LLM 重排，直接按 RRF 返回"""
        calls = _patch_pipeline(monkeypatch, n_vec=5)
        result = await ai.semantic_search("u", "查询", top_k=10, api_key="k")
        assert result["code"] == 0
        assert result["count"] == 5
        assert calls["rerank"] == 0
        assert [f["vector_rank"] for f in result["files"]] == [1, 2, 3, 4, 5]

    async def test_six_candidates_trigger_rerank(self, monkeypatch):
        """场景 9：候选数 =6 → 触发 LLM 重排（输入为 RRF 前 20）"""
        calls = _patch_pipeline(monkeypatch, n_vec=6)
        result = await ai.semantic_search("u", "查询", top_k=10, api_key="k")
        assert result["count"] == 6
        assert calls["rerank"] == 1
        rerank_query, rerank_files = calls["rerank_args"]
        assert rerank_query == "查询"
        assert len(rerank_files) == 6

    async def test_rerank_failure_falls_back_to_rrf(self, monkeypatch):
        """场景 10：LLM 重排抛异常 → 回退统一 RRF 顺序，结果完整"""
        async def exploding(query, files, api_key):
            raise RuntimeError("llm down")

        calls = _patch_pipeline(monkeypatch, n_vec=6, rerank=exploding)
        result = await ai.semantic_search("u", "查询", top_k=10, api_key="k")
        assert result["code"] == 0
        assert result["count"] == 6
        assert calls["rerank"] == 1  # 调过一次（失败）
        # 回退后仍按 rrf_score 降序
        scores = [f["rrf_score"] for f in result["files"]]
        assert scores == sorted(scores, reverse=True)

    async def test_empty_result(self, monkeypatch):
        """场景 11：两路均无结果 → count=0，不重排"""
        calls = _patch_pipeline(monkeypatch, n_vec=0, n_kw=0)
        result = await ai.semantic_search("u", "查不到的", top_k=10, api_key="k")
        assert result == {"code": 0, "count": 0, "files": []}
        assert calls["rerank"] == 0

    async def test_keyword_path_driven_by_llm_plan(self, monkeypatch):
        """关键词路输入来自 LLM 规划的 mysql_keywords（精确查询场景）"""
        calls = _patch_pipeline(monkeypatch, n_vec=2, n_kw=3, plan={
            "query_type": "exact",
            "vector_queries": ["季度报告"],
            "mysql_keywords": ["季度报告", "Q3", "xlsx"],
        })
        result = await ai.semantic_search("u", "季度报告Q3.xlsx", top_k=10, api_key="k")
        assert calls["keyword_search"] == [["季度报告", "Q3", "xlsx"]]
        assert result["count"] == 5  # 2 向量 + 3 关键词（md5 不重叠）
        # 双路并存：向量路候选带 keyword_rank=None，关键词候选 vector_rank=None
        by_md5 = {f["md5"]: f for f in result["files"]}
        assert by_md5["v0"]["vector_rank"] == 1 and by_md5["v0"]["keyword_rank"] is None
        assert by_md5["k0"]["vector_rank"] is None and by_md5["k0"]["keyword_rank"] == 1

    async def test_empty_keywords_skip_mysql_path(self, monkeypatch):
        """mysql_keywords 为空且索引可用 → 不执行关键词召回"""
        calls = _patch_pipeline(monkeypatch, n_vec=3, plan={
            "query_type": "fuzzy",
            "vector_queries": ["a", "b", "c"],
            "mysql_keywords": [],
        })
        await ai.semantic_search("u", "模糊描述", top_k=10, api_key="k")
        assert calls["keyword_search"] == []

    async def test_index_empty_keyword_fallback(self, monkeypatch):
        """索引为空且规划关键词为空 → 用原始查询兜底关键词路（新用户可按文件名搜索）"""
        calls = _patch_pipeline(monkeypatch, n_vec=0, index_ready=False)
        result = await ai.semantic_search("u", "报告.docx", top_k=10, api_key="k")
        assert calls["keyword_search"] == [["报告.docx"]]
        assert result["count"] == 0  # mock 的关键词路返回空

    async def test_vector_path_uses_plan_queries_for_embedding(self, monkeypatch):
        """向量路 embedding 输入为规划的 vector_queries（模糊多改写）"""
        plan = {"query_type": "fuzzy",
                "vector_queries": ["改写1", "改写2", "改写3"],
                "mysql_keywords": []}
        embed_calls = []

        async def fake_embed(queries, api_key, fallback_query, timing=None):
            embed_calls.append(list(queries))
            return np.zeros(8, dtype=np.float32)

        _patch_pipeline(monkeypatch, n_vec=2, plan=plan)
        monkeypatch.setattr(ai, "_embed_queries", fake_embed)
        await ai.semantic_search("u", "原始查询", top_k=10, api_key="k")
        assert embed_calls == [["改写1", "改写2", "改写3"]]
