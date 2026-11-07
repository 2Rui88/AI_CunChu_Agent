"""
AI 用量记账 —— 谁在什么时候花了多少模型调用

多用户共享部署方 DashScope Key 的审计基础：embedding / vl / chat / rerank
四类调用按用户累积记录，为后续配额与成本核算提供数据（只记事实，不做事前限制）。

设计约束：
- 记账永远不阻塞主流程：写入失败仅记日志，业务照常返回
- embedding 逐块调用按任务聚合批量记（一个文件一次 INSERT），避免逐块写放大
- 表结构走 ORM create_all 自动建表；存量库用 docker/mysql/migration_indexing_jobs.sql
"""
import logging
from typing import Iterable

from sqlalchemy import BigInteger, Column, Integer, String, TIMESTAMP, text

from app.database import Base

logger = logging.getLogger("uvicorn.error")

KIND_CHAT = "chat"
KIND_EMBEDDING = "embedding"
KIND_VL = "vl"
KIND_RERANK = "rerank"


class AiUsageLog(Base):
    __tablename__ = "ai_usage_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user = Column(String(32), nullable=False, index=True, comment="触发用户")
    kind = Column(String(16), nullable=False, comment="chat / embedding / vl / rerank")
    model = Column(String(64), default="", comment="模型名快照")
    tokens = Column(Integer, default=0, comment="token 数（API 未返回时为 0，只记次数）")
    calls = Column(Integer, default=1, comment="聚合计数（如 embedding 逐块合并为一次记录）")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        # 按用户按时间查用量（配额/报表的主路径）
        {"mysql_engine": "InnoDB"},
    )


def make_row(user: str, kind: str, model: str = "", tokens: int = 0, calls: int = 1) -> dict:
    """构造一行记账记录（内存对象，稍后批量落库）"""
    return {"user": user, "kind": kind, "model": model, "tokens": tokens, "calls": calls}


async def write_usage(rows: Iterable[dict]) -> None:
    """批量写入用量记录；独立事务提交，失败只留痕，绝不连坐调用方业务事务"""
    items = [AiUsageLog(**r) for r in rows if r.get("user")]
    if not items:
        return
    try:
        from app.database import Session
        async with Session() as db:
            db.add_all(items)
            await db.commit()
    except Exception:
        logger.exception("ai_usage_log 写入失败（%d 行），忽略", len(items))
