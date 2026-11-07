"""
向量化任务域（indexing_jobs）—— 上传即投递、后台异步执行

分层：
  models.py    任务表 ORM（表即队列：pending 行 FIFO，原子 UPDATE 领取）
  scheduler.py 投递 / 公平领取 / 超时回收 / 终态收敛（只动表，不执行）
  pipeline.py  任务执行管线（下载→提取→描述→分块→向量化→落库，自 ai.py 移植）
  worker.py    进程内消费循环（lifespan 启停，多进程安全）

设计参照「档案即队列」：不引入外置消息中间件，MySQL 一张表承担
排队/幂等/重试/超时回收，任何一环崩溃后任务表都能自愈。
"""
from app.indexing import models, pipeline, scheduler, worker  # noqa: F401
