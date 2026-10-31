"""
对比新旧搜索链路的时间埋点结果（bench_old_code.jsonl vs bench_new_code.jsonl），
按查询类型分组取中位数，输出阶段耗时对比表。
"""
import ast
import io
import json
import statistics as st
from collections import defaultdict

KIND_LABEL = {"fuzzy": "模糊查询", "exact": "精确查询", "obscure": "冷门查询(候选少)"}


def load(path):
    rows = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("kind") == "warmup":
            continue
        # timing_line: SEARCH_TIMING ... timing={...}
        tl = r["timing_line"]
        timing = ast.literal_eval(tl[tl.index("{"):])
        rows.append({"kind": r["kind"], "count": r.get("count", 0), "timing": timing})
    return rows


def merge_stages(t):
    """统一新旧阶段名：旧 = classify + rewrite 两次 LLM；新 = plan 一次"""
    return {
        "预处理(LLm规划)": t.get("query_plan_ms",
                            t.get("query_classify_ms", 0) + t.get("query_rewrite_ms", 0)),
        "embedding": t.get("embedding_ms", 0),
        "faiss": t.get("faiss_ms", 0),
        "mysql关键词": t.get("mysql_keyword_ms", 0),
        "mysql向量回查": t.get("mysql_vector_join_ms", 0),
        "rrf合并": t.get("rrf_ms", 0),
        "llm重排": t.get("llm_rerank_ms", 0),
        "total": t.get("total_ms", 0),
    }


def main():
    old = load("testdocs/bench_old_code.jsonl")
    new = load("testdocs/bench_new_code.jsonl")

    stages = list(merge_stages(old[0]["timing"]).keys())

    for kind in ("fuzzy", "exact", "obscure"):
        o = [r for r in old if r["kind"] == kind]
        n = [r for r in new if r["kind"] == kind]
        if not o and not n:
            continue
        print(f"\n== {KIND_LABEL[kind]} ==")
        print(f"   样本: 旧 {len(o)} 次 (候选数 {[r['count'] for r in o]}) / "
              f"新 {len(n)} 次 (候选数 {[r['count'] for r in n]})")
        header = f"   {'阶段':<14}{'旧(ms)':>10}{'新(ms)':>10}{'变化':>10}"
        print(header)
        for stage in stages:
            ov = st.median([merge_stages(r["timing"])[stage] for r in o]) if o else None
            nv = st.median([merge_stages(r["timing"])[stage] for r in n]) if n else None
            if ov is None or nv is None:
                print(f"   {stage:<14}{ov if ov is not None else '-':>10}{nv if nv is not None else '-':>10}")
                continue
            delta = f"{(nv - ov) / 1000:+.1f}s"
            print(f"   {stage:<14}{ov:>10.0f}{nv:>10.0f}{delta:>10}")

    # 汇总
    print("\n== 全部查询汇总（中位数） ==")
    for label, rows in (("旧", old), ("新", new)):
        tot = st.median([r["timing"]["total_ms"] for r in rows])
        pre = st.median([merge_stages(r["timing"])["预处理(LLm规划)"] for r in rows])
        rr = st.median([r["timing"]["llm_rerank_ms"] for r in rows])
        skipped = sum(1 for r in rows if r["timing"]["llm_rerank_ms"] == 0.0)
        print(f"   {label}: total={tot/1000:.1f}s  预处理={pre/1000:.1f}s  "
              f"重排={rr/1000:.1f}s(中位)  跳过重排 {skipped}/{len(rows)} 次")


if __name__ == "__main__":
    main()
