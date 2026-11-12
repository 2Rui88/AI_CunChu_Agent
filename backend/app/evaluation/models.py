"""RAG 检索评估表的 ORM 定义（eval_datasets / eval_runs 两族四表）"""
from sqlalchemy import (
    JSON, BigInteger, Column, Integer, SmallInteger, String, Text, TIMESTAMP,
    UniqueConstraint, text,
)

from app.database import Base


class EvalDataset(Base):
    """
    一个评估数据集：一组固定问题 + 期望命中的目标文件（金标）。

    设计参照 Yuxi 的 evaluation_datasets：题目独立成行（而非整包 JSON 落库），
    逐题参与评估；has_gold / has_gold_answer 标记数据集能力，前端据此提示
    可计算的指标。归属用户（评估的是"该用户自己文件库"的检索质量）。
    """
    __tablename__ = "eval_datasets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(String(64), unique=True, nullable=False, index=True)
    user = Column(String(32), nullable=False, index=True, comment="所属用户")
    name = Column(String(128), nullable=False)
    description = Column(String(512), default="")
    item_count = Column(Integer, nullable=False, default=0)
    has_gold = Column(SmallInteger, nullable=False, default=0,
                      comment="1=存在带 gold_md5s 的题目，可算检索指标")
    has_gold_answer = Column(SmallInteger, nullable=False, default=0,
                             comment="1=存在带参考答案的题目（留给答案评估阶段）")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = ({"mysql_engine": "InnoDB"},)


class EvalDatasetItem(Base):
    """评估数据集单题：query 必填；gold_md5s（目标文件）与 gold_answer 按需。

    gold_files（文件名形式金标）在上传时已解析为 md5，此处只存归一结果；
    gold_names 与 gold_md5s 平行，仅用于人读展示。
    """
    __tablename__ = "eval_dataset_items"
    __table_args__ = (
        UniqueConstraint("dataset_id", "item_index", name="uq_eval_item_dataset_index"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(String(64), nullable=False, index=True)
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_md5s = Column(JSON, default=list)
    gold_names = Column(JSON, default=list)
    gold_answer = Column(Text)


class EvalRun(Base):
    """一次评估运行：对某数据集逐题执行真实检索链路并聚合指标。

    status 只有 running / completed——逐题结果落在 eval_run_items，
    最后一题完成（或续跑发现已无缺口）时聚合收口。无后台任务：
    推进由前端循环调 /eval/run/step 驱动，中断后随时可续。
    config 快照记录当次检索相关配置，保证两次运行可比。
    """
    __tablename__ = "eval_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    dataset_id = Column(String(64), nullable=False, index=True)
    user = Column(String(32), nullable=False, index=True)
    name = Column(String(128), default="")
    status = Column(String(16), nullable=False, default="running", index=True)
    top_k = Column(Integer, nullable=False, default=10)
    config = Column(JSON, default=dict, comment="启动时的检索配置快照")
    metrics = Column(JSON, default=dict, comment="完成后的聚合指标")
    total_items = Column(Integer, nullable=False, default=0)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    finish_time = Column(TIMESTAMP, nullable=True)
    __table_args__ = ({"mysql_engine": "InnoDB"},)


class EvalRunItem(Base):
    """评估逐题结果：冗余快照 query / gold（数据集删除后 run 仍自包含）。

    retrieved 存当次检索 Top-K 明细（含两路排名与融合分数，便于定位
    "规划错 / 召回弱 / 重排排丢"）；error 非空表示该题执行失败，
    不参与聚合指标，单独计数。
    """
    __tablename__ = "eval_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_index", name="uq_eval_run_item_run_index"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, index=True)
    item_index = Column(Integer, nullable=False)
    query_text = Column(Text, nullable=False)
    gold_md5s = Column(JSON, default=list)
    gold_names = Column(JSON, default=list)
    gold_answer = Column(Text)
    retrieved = Column(JSON, default=list)
    metrics = Column(JSON, default=dict)
    error = Column(String(512), default="")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
