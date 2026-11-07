"""向量化任务表的 ORM 定义（indexing_jobs）"""
from sqlalchemy import (
    BigInteger, Column, Integer, SmallInteger, String, TIMESTAMP, UniqueConstraint, text,
)

from app.database import Base


class IndexingJob(Base):
    """
    一个文件的一次「AI 描述 + 向量化」任务。

    生命周期：pending → running → done / failed
      - md5 唯一约束：同一文件同时最多一个任务（多用户秒传同一文件只触发一次）
      - failed 后：自动重试由 attempts 控制（< max 时回 pending）；
        超限后由用户手动重建（重置行重新入队），避免失败风暴
      - running 超时（消费者崩溃）由调度器回收重投

    表本身即队列：pending 行按 id FIFO 排队，原子 UPDATE 领取，
    不依赖任何外置消息中间件（参照「档案即队列」模式）。
    """
    __tablename__ = "indexing_jobs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    md5 = Column(String(256), nullable=False, comment="文件内容 MD5，任务幂等键")
    user = Column(String(32), nullable=False, index=True,
                  comment="触发者（公平调度 + 展示归属）")
    filename = Column(String(128), default="")
    file_type = Column(String(32), default="")
    force = Column(SmallInteger, default=0, comment="1=手动重建：清旧切片全量重做")
    status = Column(String(16), nullable=False, default="pending", index=True,
                    comment="pending / running / done / failed")
    attempts = Column(Integer, nullable=False, default=0, comment="已执行次数（含首次）")
    error = Column(String(512), default="", comment="最后一次失败的错误摘要")
    worker = Column(String(64), default="", comment="当前/最后领取者标识")
    started_at = Column(TIMESTAMP, nullable=True, comment="最近一次领取时间（超时回收依据）")
    finished_at = Column(TIMESTAMP, nullable=True, comment="终态时间")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    update_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"),
                         onupdate=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("md5", name="uq_indexing_job_md5"),
        {"mysql_engine": "InnoDB"},
    )

    def public_dict(self) -> dict:
        """对外状态视图（不含内部字段）"""
        return {
            "md5": self.md5,
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error or "",
            "create_time": str(self.create_time or ""),
            "finished_at": str(self.finished_at or ""),
        }
