"""RAG 检索评估接口 —— 数据集管理 + 评估运行推进/查询（全部同步，无后台任务）"""

import logging

from fastapi import APIRouter

from app.dependencies import check_token
from app.evaluation import service as eval_service

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["eval"])

# 上传正文过大直接拒绝（JSONL 题量上限 200，正常远小于该值）
MAX_UPLOAD_CHARS = 512 * 1024


def _auth(body: dict) -> tuple[str, str] | None:
    user = str(body.get("user") or "")
    token = str(body.get("token") or "")
    if not user or not token:
        return None
    return user, token


# ---------- 数据集 ----------

@router.post("/eval/dataset/upload")
async def eval_dataset_upload(body: dict):
    """上传评估数据集（JSONL 文本：query 必填，gold_md5s/gold_files/gold_answer 可选）"""
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    user, _ = auth
    content = str(body.get("content") or "")
    if len(content) > MAX_UPLOAD_CHARS:
        return {"code": 1, "msg": "数据集内容过大"}
    try:
        result = await eval_service.upload_dataset(
            user, str(body.get("name") or ""), str(body.get("description") or ""), content)
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("上传评估数据集失败")
        return {"code": 1, "msg": f"上传失败: {e}"}
    return {"code": 0, **result}


@router.post("/eval/dataset/list")
async def eval_dataset_list(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        return {"code": 0, "datasets": await eval_service.list_datasets(auth[0])}
    except Exception as e:
        logger.exception("获取评估数据集列表失败")
        return {"code": 1, "msg": f"获取失败: {e}"}


@router.post("/eval/dataset/items")
async def eval_dataset_items(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        return {"code": 0, "items": await eval_service.dataset_items(
            auth[0], str(body.get("dataset_id") or ""))}
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("获取评估数据集题目失败")
        return {"code": 1, "msg": f"获取失败: {e}"}


@router.post("/eval/dataset/delete")
async def eval_dataset_delete(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        await eval_service.delete_dataset(auth[0], str(body.get("dataset_id") or ""))
        return {"code": 0}
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("删除评估数据集失败")
        return {"code": 1, "msg": f"删除失败: {e}"}


# ---------- 评估运行 ----------

@router.post("/eval/run/start")
async def eval_run_start(body: dict):
    """创建评估运行（只建 run 行不执行；逐题推进由 /eval/run/step 完成）"""
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        top_k = int(body.get("top_k") or 10)
    except (TypeError, ValueError):
        top_k = 10
    try:
        result = await eval_service.start_run(
            auth[0], str(body.get("dataset_id") or ""), top_k)
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("创建评估运行失败")
        return {"code": 1, "msg": f"创建失败: {e}"}
    return {"code": 0, **result}


@router.post("/eval/run/step")
async def eval_run_step(body: dict):
    """执行下一题（同步跑真实检索链路，单题一个请求；done=true 表示整轮结束）"""
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        result = await eval_service.step_run(auth[0], str(body.get("run_id") or ""))
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("评估执行失败")
        return {"code": 1, "msg": f"评估执行失败: {e}"}
    return {"code": 0, **result}


@router.post("/eval/run/list")
async def eval_run_list(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        return {"code": 0, "runs": await eval_service.list_runs(auth[0])}
    except Exception as e:
        logger.exception("获取评估运行列表失败")
        return {"code": 1, "msg": f"获取失败: {e}"}


@router.post("/eval/run/detail")
async def eval_run_detail(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        return {"code": 0, **await eval_service.run_detail(
            auth[0], str(body.get("run_id") or ""))}
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("获取评估运行详情失败")
        return {"code": 1, "msg": f"获取失败: {e}"}


@router.post("/eval/run/delete")
async def eval_run_delete(body: dict):
    auth = _auth(body)
    if auth is None or not await check_token(*auth):
        return {"code": 4}
    try:
        await eval_service.delete_run(auth[0], str(body.get("run_id") or ""))
        return {"code": 0}
    except ValueError as e:
        return {"code": 1, "msg": str(e)}
    except Exception as e:
        logger.exception("删除评估运行失败")
        return {"code": 1, "msg": f"删除失败: {e}"}
