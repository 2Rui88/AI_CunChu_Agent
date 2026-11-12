"""文件级检索评估指标（纯函数，无 IO）。

评估对象与 semantic_search 的返回一致：最终按 md5 合并后的有序文件列表。
K 值固定取 1 / 5 / 10，仅对 K <= top_k 的档位出分（top_k=5 时无 recall@10，
聚合按"有该指标的题"取均值，同一 run 内所有题档位一致）。
"""
from __future__ import annotations

K_VALUES = (1, 5, 10)


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    """前 K 个结果覆盖金标文件的比例；无金标返回 0"""
    if not gold or not retrieved[:k]:
        return 0.0
    return len(set(retrieved[:k]) & gold) / len(gold)


def first_hit_rank(retrieved: list[str], gold: set[str]) -> int | None:
    """第一个金标文件的排名（1 起）；未命中返回 None"""
    for rank, md5 in enumerate(retrieved, start=1):
        if md5 in gold:
            return rank
    return None


def mrr(retrieved: list[str], gold: set[str]) -> float:
    """平均倒数排名：首个命中金标的排名取倒数；未命中记 0"""
    rank = first_hit_rank(retrieved, gold)
    return 0.0 if rank is None else 1.0 / rank


def question_metrics(retrieved_md5s: list[str], gold_md5s: list[str],
                     top_k: int) -> dict:
    """单题指标：recall@K（K <= top_k 的档位）+ MRR + 首中排名"""
    gold = {m for m in (gold_md5s or []) if m}
    result: dict = {
        "mrr": round(mrr(retrieved_md5s, gold), 4),
        "first_hit_rank": first_hit_rank(retrieved_md5s, gold),
    }
    for k in K_VALUES:
        if k > top_k:
            break
        result[f"recall@{k}"] = round(recall_at_k(retrieved_md5s, gold, k), 4)
    return result


def aggregate_metrics(item_metrics: list[dict], *, total_items: int,
                      error_count: int) -> dict:
    """跨题聚合：对每项指标取"拥有该指标的题"的均值 + 执行统计。

    执行失败（error）的题不进均值但计入 errors；无金标的题不产生
    检索指标，同样不进均值——分母是"有效题数"，不是 total_items。
    """
    overall: dict = {"total_items": total_items, "evaluated": len(item_metrics),
                     "errors": error_count}
    valid = [m for m in item_metrics if m]
    if not valid:
        return overall
    keys = dict.fromkeys(key for m in valid for key in m if key != "first_hit_rank")
    for key in keys:
        values = [m[key] for m in valid if key in m]
        if values:
            overall[key] = round(sum(values) / len(values), 4)
    return overall
