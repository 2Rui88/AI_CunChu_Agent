"""RAG 检索评估编排：数据集生命周期 + 逐题评估推进。

无后台任务/队列：一次评估（run）由前端循环调用 step_run 逐题推进，
每题在单个 HTTP 请求内同步执行真实 semantic_search 链路并落库；
中断后再次调用自然续跑，最后一题完成时聚合收口。
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import delete, func, select, text

from app.config import settings
from app.database import Session
from app.evaluation.metrics import aggregate_metrics, question_metrics
from app.evaluation.models import (
    EvalDataset, EvalDatasetItem, EvalRun, EvalRunItem,
)
from app.models import UserFileList
from app.routers.ai import SCORE_THRESHOLD, semantic_search

# 与 semantic_search 链路中的固定行为保持一致（快照进 run.config，
# 让两次运行的可比性不依赖"当时代码里写的是多少"）
RERANK_TRIGGER = 5      # 候选数 > 5 才触发 LLM 重排
RERANK_CANDIDATES = 20  # 重排取 RRF 前多少名
RRF_K = 60              # RRF 融合常数

MAX_DATASET_ITEMS = 200
DEFAULT_TOP_K = 10
MAX_TOP_K = 20

# 评估明细每个检索结果保留的字段（semantic_search 返回的文件对象子集）
_RETRIEVED_FIELDS = (
    "md5", "filename", "score", "vector_rank", "keyword_rank",
    "rrf_score", "source", "match_context", "chunk_index",
)


def _parse_jsonl(content: str) -> list[dict[str, Any]]:
    """解析 JSONL 题目：query 必填；gold_md5s / gold_files / gold_answer 可选。

    带行号的报错直接抛 ValueError，由路由层返回给上传者。
    """
    items: list[dict[str, Any]] = []
    for line_num, line in enumerate((content or "").strip().splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"第 {line_num} 行 JSON 格式错误: {e}")
        if not isinstance(obj, dict) or not str(obj.get("query") or "").strip():
            raise ValueError(f"第 {line_num} 行缺少必需的 query 字段")
        items.append({
            "query": str(obj["query"]).strip(),
            "gold_md5s": [str(m).strip() for m in (obj.get("gold_md5s") or []) if str(m).strip()],
            "gold_files": [str(n).strip() for n in (obj.get("gold_files") or []) if str(n).strip()],
            "gold_answer": (str(obj["gold_answer"]).strip() or None) if obj.get("gold_answer") else None,
        })
    if not items:
        raise ValueError("文件中没有有效题目（每行一个 JSON 对象，至少需要 query 字段）")
    if len(items) > MAX_DATASET_ITEMS:
        raise ValueError(f"题目数量超过上限 {MAX_DATASET_ITEMS}（当前 {len(items)}）")
    return items


async def _resolve_gold(user: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 gold_files 文件名解析为 md5（在该用户自己的文件里精确匹配）。

    同名多文件时全部并入金标（每个都是合法命中）；一个都匹配不到的
    文件名直接报错回绝上传——静默记 0 分会让指标无法解释。
    gold_md5s 原样保留并与解析结果合并去重。
    """
    wanted_names = sorted({n for it in items for n in it["gold_files"]})
    md5s_by_name: dict[str, list[str]] = {n: [] for n in wanted_names}
    if wanted_names:
        async with Session() as db:
            rows = (await db.execute(
                select(UserFileList.file_name, UserFileList.md5)
                .where(UserFileList.user == user, UserFileList.file_name.in_(wanted_names))
            )).all()
            for name, md5 in rows:
                md5s_by_name.setdefault(name, []).append(md5)

    unresolved = [n for n in wanted_names if not md5s_by_name[n]]
    if unresolved:
        raise ValueError(
            "以下 gold_files 在你的文件列表中不存在，请核对文件名："
            + "、".join(unresolved[:10])
            + (f" 等 {len(unresolved)} 个" if len(unresolved) > 10 else "")
        )

    # md5 -> 展示名（取最新一条，仅用于人读）
    name_by_md5: dict[str, str] = {}
    for it in items:
        for n in it["gold_files"]:
            for md5 in md5s_by_name[n]:
                name_by_md5.setdefault(md5, n)

    resolved: list[dict[str, Any]] = []
    for it in items:
        gold_md5s = list(dict.fromkeys(it["gold_md5s"] + [
            md5 for n in it["gold_files"] for md5 in md5s_by_name[n]
        ]))
        gold_names = [name_by_md5.get(md5, "") for md5 in gold_md5s]
        resolved.append({
            "query": it["query"],
            "gold_md5s": gold_md5s,
            "gold_names": gold_names,
            "gold_answer": it["gold_answer"],
        })
    return resolved


# ---------- 数据集 ----------

async def upload_dataset(user: str, name: str, description: str,
                         content: str) -> dict[str, Any]:
    items = await _resolve_gold(user, _parse_jsonl(content))
    dataset_id = f"ds_{secrets.token_hex(6)}"
    has_gold = any(it["gold_md5s"] for it in items)
    has_gold_answer = any(it["gold_answer"] for it in items)
    async with Session() as db:
        db.add_all([EvalDataset(
            dataset_id=dataset_id, user=user,
            name=(name or "").strip() or dataset_id,
            description=(description or "").strip()[:512],
            item_count=len(items),
            has_gold=int(has_gold), has_gold_answer=int(has_gold_answer),
        )] + [EvalDatasetItem(
            dataset_id=dataset_id, item_index=index,
            query_text=it["query"], gold_md5s=it["gold_md5s"],
            gold_names=it["gold_names"], gold_answer=it["gold_answer"],
        ) for index, it in enumerate(items)])
        await db.commit()
    return {"dataset_id": dataset_id, "item_count": len(items),
            "has_gold": has_gold, "has_gold_answer": has_gold_answer}


async def _get_own_dataset(user: str, dataset_id: str) -> EvalDataset:
    async with Session() as db:
        row = (await db.execute(
            select(EvalDataset).where(
                EvalDataset.dataset_id == dataset_id, EvalDataset.user == user)
        )).scalar()
    if row is None:
        raise ValueError("数据集不存在或不属于当前用户")
    return row


async def list_datasets(user: str) -> list[dict[str, Any]]:
    async with Session() as db:
        rows = (await db.execute(
            select(EvalDataset).where(EvalDataset.user == user)
            .order_by(EvalDataset.id.desc())
        )).scalars().all()
    return [{
        "dataset_id": r.dataset_id, "name": r.name, "description": r.description,
        "item_count": r.item_count, "has_gold": bool(r.has_gold),
        "has_gold_answer": bool(r.has_gold_answer),
        "create_time": str(r.create_time) if r.create_time else "",
    } for r in rows]


async def dataset_items(user: str, dataset_id: str) -> list[dict[str, Any]]:
    await _get_own_dataset(user, dataset_id)
    async with Session() as db:
        rows = (await db.execute(
            select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == dataset_id)
            .order_by(EvalDatasetItem.item_index)
        )).scalars().all()
    return [{
        "item_index": r.item_index, "query": r.query_text,
        "gold_md5s": r.gold_md5s or [], "gold_names": r.gold_names or [],
        "gold_answer": r.gold_answer or "",
    } for r in rows]


async def delete_dataset(user: str, dataset_id: str) -> None:
    """删除数据集及其全部运行记录（运行行的题目快照随之消失，
    保留孤儿 run 没有比对价值，一并清理）"""
    await _get_own_dataset(user, dataset_id)
    async with Session() as db:
        run_ids = (await db.execute(
            select(EvalRun.run_id).where(EvalRun.dataset_id == dataset_id)
        )).scalars().all()
        if run_ids:
            await db.execute(delete(EvalRunItem).where(EvalRunItem.run_id.in_(run_ids)))
            await db.execute(delete(EvalRun).where(EvalRun.run_id.in_(run_ids)))
        await db.execute(delete(EvalDatasetItem).where(EvalDatasetItem.dataset_id == dataset_id))
        await db.execute(delete(EvalDataset).where(EvalDataset.dataset_id == dataset_id))
        await db.commit()


# ---------- 评估运行 ----------

def _config_snapshot(top_k: int) -> dict[str, Any]:
    return {
        "top_k": top_k,
        "chat_model": settings.chat_model,
        "embedding_model": settings.embedding_model,
        "score_threshold": SCORE_THRESHOLD,
        "rrf_k": RRF_K,
        "rerank_trigger": RERANK_TRIGGER,
        "rerank_candidates": RERANK_CANDIDATES,
    }


async def start_run(user: str, dataset_id: str, top_k: int) -> dict[str, Any]:
    dataset = await _get_own_dataset(user, dataset_id)
    top_k = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    run_id = f"run_{secrets.token_hex(6)}"
    async with Session() as db:
        db.add(EvalRun(
            run_id=run_id, dataset_id=dataset_id, user=user,
            name=f"{dataset.name} · {run_id[4:10]}", top_k=top_k,
            config=_config_snapshot(top_k), metrics={},
            total_items=dataset.item_count,
        ))
        await db.commit()
    return {"run_id": run_id, "total_items": dataset.item_count, "top_k": top_k}


async def _get_own_run(user: str, run_id: str) -> EvalRun:
    async with Session() as db:
        row = (await db.execute(
            select(EvalRun).where(EvalRun.run_id == run_id, EvalRun.user == user)
        )).scalar()
    if row is None:
        raise ValueError("评估运行不存在或不属于当前用户")
    return row


def _run_item_public(row: EvalRunItem) -> dict[str, Any]:
    return {
        "item_index": row.item_index, "query": row.query_text,
        "gold_md5s": row.gold_md5s or [], "gold_names": row.gold_names or [],
        "retrieved": row.retrieved or [], "metrics": row.metrics or {},
        "error": row.error or "",
    }


async def _finalize_if_complete(run: EvalRun) -> dict[str, Any] | None:
    """全部题目已有结果时聚合收口：更新 run 状态并返回聚合指标。"""
    async with Session() as db:
        rows = (await db.execute(
            select(EvalRunItem.metrics, EvalRunItem.error)
            .where(EvalRunItem.run_id == run.run_id)
            .order_by(EvalRunItem.item_index)
        )).all()
        if len(rows) < (run.total_items or 0):
            return None
        overall = aggregate_metrics(
            [m for m, _ in rows], total_items=run.total_items,
            error_count=sum(1 for _, err in rows if err),
        )
        await db.execute(
            EvalRun.__table__.update()
            .where(EvalRun.run_id == run.run_id)
            .values(status="completed", metrics=overall,
                    finish_time=text("CURRENT_TIMESTAMP"))
        )
        await db.commit()
    return overall


async def step_run(user: str, run_id: str) -> dict[str, Any]:
    """推进一题：找到第一个还没有结果的题目，同步执行检索链路并落库。

    无缺口时收口（正常收尾或崩溃后续跑的兜底）。返回体携带本题明细
    与进度，done=true 表示整轮结束并带聚合指标。
    """
    run = await _get_own_run(user, run_id)
    if run.status != "running":
        return {"done": True, "completed": run.total_items,
                "total": run.total_items, "item": None, "metrics": run.metrics or {}}

    async with Session() as db:
        dataset_items = (await db.execute(
            select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == run.dataset_id)
            .order_by(EvalDatasetItem.item_index)
        )).scalars().all()
        done_indexes = set((await db.execute(
            select(EvalRunItem.item_index).where(EvalRunItem.run_id == run.run_id)
        )).scalars().all())
    pending = [it for it in dataset_items if it.item_index not in done_indexes]
    if not pending:
        overall = await _finalize_if_complete(run)
        return {"done": True, "completed": len(dataset_items),
                "total": len(dataset_items), "item": None,
                "metrics": overall or run.metrics or {}}

    item = pending[0]
    retrieved: list[dict[str, Any]] = []
    error = ""
    metrics: dict[str, Any] = {}
    try:
        result = await semantic_search(user, item.query_text, top_k=run.top_k)
        if result.get("code") != 0:
            error = str(result.get("msg") or "检索失败")[:500]
        else:
            for rank, f in enumerate(result.get("files") or [], start=1):
                retrieved.append({"rank": rank, **{
                    key: f.get(key) for key in _RETRIEVED_FIELDS}})
            if item.gold_md5s:
                metrics = question_metrics(
                    [r["md5"] for r in retrieved], item.gold_md5s, run.top_k)
    except Exception as e:  # 单题失败不中断整轮：记 error，聚合时单独计数
        error = f"{type(e).__name__}: {e}"[:500]

    async with Session() as db:
        db.add(EvalRunItem(
            run_id=run.run_id, item_index=item.item_index,
            query_text=item.query_text, gold_md5s=item.gold_md5s,
            gold_names=item.gold_names, gold_answer=item.gold_answer,
            retrieved=retrieved, metrics=metrics, error=error,
        ))
        await db.commit()

    completed = len(done_indexes) + 1
    done = completed >= len(dataset_items)
    overall = await _finalize_if_complete(run) if done else None
    return {
        "done": done, "completed": completed, "total": len(dataset_items),
        "item": {"item_index": item.item_index, "query": item.query_text,
                 "gold_md5s": item.gold_md5s or [],
                 "gold_names": item.gold_names or [],
                 "retrieved": retrieved, "metrics": metrics, "error": error},
        "metrics": overall or {},
    }


async def list_runs(user: str) -> list[dict[str, Any]]:
    async with Session() as db:
        rows = (await db.execute(
            select(EvalRun).where(EvalRun.user == user).order_by(EvalRun.id.desc())
        )).scalars().all()
        # running 的运行实时统计已评题数（不落库，读时算）
        counts: dict[str, int] = {}
        for r in rows:
            if r.status == "running":
                counts[r.run_id] = (await db.execute(
                    select(func.count()).select_from(EvalRunItem)
                    .where(EvalRunItem.run_id == r.run_id)
                )).scalar() or 0
    return [{
        "run_id": r.run_id, "name": r.name, "dataset_id": r.dataset_id,
        "status": r.status, "top_k": r.top_k, "config": r.config or {},
        "metrics": r.metrics or {}, "total_items": r.total_items,
        "completed_items": counts.get(r.run_id, r.total_items if r.status == "completed" else 0),
        "create_time": str(r.create_time) if r.create_time else "",
        "finish_time": str(r.finish_time) if r.finish_time else "",
    } for r in rows]


async def run_detail(user: str, run_id: str) -> dict[str, Any]:
    run = await _get_own_run(user, run_id)
    async with Session() as db:
        rows = (await db.execute(
            select(EvalRunItem).where(EvalRunItem.run_id == run_id)
            .order_by(EvalRunItem.item_index)
        )).scalars().all()
    return {
        "run_id": run.run_id, "name": run.name, "dataset_id": run.dataset_id,
        "status": run.status, "top_k": run.top_k, "config": run.config or {},
        "metrics": run.metrics or {}, "total_items": run.total_items,
        "completed_items": len(rows),
        "create_time": str(run.create_time) if run.create_time else "",
        "finish_time": str(run.finish_time) if run.finish_time else "",
        "items": [_run_item_public(r) for r in rows],
    }


async def delete_run(user: str, run_id: str) -> None:
    await _get_own_run(user, run_id)
    async with Session() as db:
        await db.execute(delete(EvalRunItem).where(EvalRunItem.run_id == run_id))
        await db.execute(delete(EvalRun).where(EvalRun.run_id == run_id))
        await db.commit()
