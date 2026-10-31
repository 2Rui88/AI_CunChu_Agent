"""
搜索链路计时基准 —— 在 tc_backend 容器内直调 semantic_search，
捕获 SEARCH_TIMING 阶段埋点日志，输出 JSON 行供新旧代码对比。

用法: docker exec tc_backend python /tmp/bench_search.py
"""
import asyncio
import json
import logging
import sys
import time

sys.path.insert(0, "/app")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = _Capture()
lg = logging.getLogger("uvicorn.error")
lg.addHandler(cap)
lg.setLevel(logging.INFO)

from app.routers.ai import semantic_search  # noqa: E402

USER = "zcode_doc_test"
QUERIES = [
    ("fuzzy", "讲向量检索和语义匹配的那份资料"),
    ("fuzzy", "容器和云原生运维方面的实践内容"),
    ("exact", "分布式系统一致性理论与共识算法.docx"),
    ("obscure", "财务报销制度"),
]
REPS = 3


async def run_once(kind, q):
    cap.lines.clear()
    t0 = time.perf_counter()
    r = await semantic_search(USER, q, top_k=10)
    wall = round((time.perf_counter() - t0) * 1000, 1)
    timing_line = next((l for l in cap.lines if l.startswith("SEARCH_TIMING")), "")
    row = {
        "kind": kind, "query": q, "wall_ms": wall,
        "code": r.get("code"), "count": r.get("count"),
        "top3": [f.get("filename") for f in (r.get("files") or [])[:3]],
        "timing_line": timing_line,
    }
    print(json.dumps(row, ensure_ascii=False), flush=True)


async def main():
    # 预热：触发可能的脏索引重建 + 建立 DB/网络连接，避免计入首查冷启动
    for kind, q in QUERIES:
        await run_once("warmup", q)
    for kind, q in QUERIES:
        for i in range(REPS):
            await run_once(kind, q)


asyncio.run(main())
