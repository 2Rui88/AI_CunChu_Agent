"""文件级检索评估指标的单元测试（纯函数，无 DB / 网络）"""

from app.evaluation.metrics import (
    aggregate_metrics, first_hit_rank, mrr, question_metrics, recall_at_k,
)


class TestSingleMetrics:
    def test_recall_full_hit(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 5) == 1.0

    def test_recall_partial(self):
        assert recall_at_k(["a", "x"], {"a", "b"}, 5) == 0.5

    def test_recall_respects_k_cutoff(self):
        # 金标在第 3 位：recall@2 不含，recall@3 含
        assert recall_at_k(["x", "y", "a"], {"a"}, 2) == 0.0
        assert recall_at_k(["x", "y", "a"], {"a"}, 3) == 1.0

    def test_recall_no_gold(self):
        assert recall_at_k(["a"], set(), 5) == 0.0

    def test_first_hit_rank(self):
        assert first_hit_rank(["x", "a", "b"], {"a", "b"}) == 2
        assert first_hit_rank(["x", "y"], {"a"}) is None

    def test_mrr(self):
        assert mrr(["x", "a"], {"a"}) == 0.5
        assert mrr(["a"], {"a"}) == 1.0
        assert mrr(["x"], {"a"}) == 0.0


class TestQuestionMetrics:
    def test_k_gated_by_top_k(self):
        m = question_metrics(["a", "b"], ["a"], top_k=5)
        assert "recall@1" in m and "recall@5" in m
        assert "recall@10" not in m  # top_k=5 不出 @10 档位

    def test_no_gold_gives_zero_metrics_but_keeps_shape(self):
        m = question_metrics(["a"], [], top_k=10)
        assert m["recall@10"] == 0.0
        assert m["first_hit_rank"] is None

    def test_typical_hit(self):
        m = question_metrics(["x", "y", "a"], ["a"], top_k=10)
        assert m["first_hit_rank"] == 3
        assert m["mrr"] == round(1 / 3, 4)
        assert m["recall@1"] == 0.0
        assert m["recall@5"] == 1.0


class TestAggregate:
    def test_aggregate_averages_and_counts(self):
        m1 = question_metrics(["a", "b"], ["a"], top_k=10)
        m2 = question_metrics(["x", "b"], ["b"], top_k=10)
        overall = aggregate_metrics([m1, m2], total_items=3, error_count=1)
        assert overall["total_items"] == 3
        assert overall["evaluated"] == 2
        assert overall["errors"] == 1
        assert overall["recall@1"] == 0.5
        assert overall["mrr"] == 0.75
        assert "first_hit_rank" not in overall  # 非均值指标不聚合

    def test_aggregate_empty(self):
        overall = aggregate_metrics([], total_items=0, error_count=0)
        assert overall == {"total_items": 0, "evaluated": 0, "errors": 0}
