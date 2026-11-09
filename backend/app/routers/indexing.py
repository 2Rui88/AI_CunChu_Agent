"""
向量化任务状态查询 —— 前端上传/重建后轮询此端点展示进度

投递入口仍是 POST /api/ai/describe（内部改为写任务行立即返回）；
本路由只读 indexing_jobs 的任务状态与全局聚合，不触发任何执行。
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.database import Session
from app.dependencies import check_token
from app.indexing import scheduler

router = APIRouter(prefix="/api/indexing", tags=["indexing"])


class StatusRequest(BaseModel):
    user: str = ""
    token: str = ""
    md5: str = ""


@router.post("/status")
async def indexing_status(body: StatusRequest):
    """查询指定文件的向量化任务状态（仅任务触发者可查）"""
    if not await check_token(body.user, body.token):
        return {"code": 4}

    async with Session() as db:
        job = await scheduler.get_status(db, user=body.user, md5=body.md5)
    if job is None:
        return {"code": 1, "msg": "no job for this file"}
    return {"code": 0, "job": job}


@router.post("/stats")
async def indexing_stats(body: StatusRequest):
    """
    队列全局聚合统计（监控最小版；仅登录用户可查，输出不含任何用户名）。

    判定口径：最老 pending 年龄持续增长 && running==0 ⇒ 没有活着的消费者
    （容器 up 但没人消费：开关配错/消费循环卡死，Docker 健康检查看不见）。
    """
    if not await check_token(body.user, body.token):
        return {"code": 4}

    async with Session() as db:
        stats = await scheduler.queue_stats(db)
    return {"code": 0, "queue": stats}
