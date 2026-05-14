"""
AI 智能检索模块 —— describe / search / rebuild

describe: 生成文件 AI 描述 + 向量 → 存入 MySQL + FAISS
search:   自然语言查询 → embedding → FAISS 检索 → 回查 MySQL 返回结果
rebuild:  从 MySQL 全量重建用户 FAISS 索引
"""
import numpy as np
from fastapi import APIRouter
from sqlalchemy import select
from app.database import Session
from app.models import FileInfo, UserFileList, FileAiDesc, UserFileAiDesc
from app.dependencies import check_token
from app.config import settings
from app.dashscope_client import describe_image, get_embedding
from app.faiss_service import (
    search as faiss_search,
    add_vector,
    rebuild_from_db,
    l2_normalize,
    get_ntotal,
    load_index, save_index,
)

router = APIRouter(prefix="/api", tags=["ai"])

# 兼容旧 C 项目的分数阈值
SCORE_THRESHOLD = 0.45


def _build_public_url(db_url: str) -> str:
    """
    将数据库中的文件 URL 替换为公网可访问的 URL。
    DashScope Qwen-VL 需要能直接下载图片，所以必须用公网地址。
    如果未配置 public_server_ip，则回退到 web_server_ip（仅 Docker 内网可用）。
    """
    ip = settings.public_server_ip or settings.web_server_ip
    port = settings.public_server_port or settings.web_server_port

    path_part = db_url
    for prefix in ("http://", "https://"):
        if path_part.startswith(prefix):
            path_part = path_part[len(prefix):]
            pos = path_part.find("/")
            if pos > 0:
                path_part = path_part[pos:]
            break

    return f"http://{ip}:{port}{path_part}"


# ============================================================
#  describe —— AI 文件描述 + 向量化
# ============================================================

@router.post("/ai/describe")
async def ai_describe(body: dict):
    """
    为指定文件生成 AI 描述和向量，存入 file_ai_desc（全局缓存）和
    user_file_ai_desc（用户记录），并追加到用户 FAISS 索引。
    需要: user, token, md5, filename, type, api_key
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    api_key = body.get("api_key", "")
    if not api_key:
        return {"code": 1, "msg": "missing api_key"}

    md5_val = body.get("md5", "")
    filename = body.get("filename", "")
    file_type = body.get("type", "")
    force = body.get("force", False)

    async with Session() as db:
        # 校验用户是否拥有该文件
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user, UserFileList.md5 == md5_val
            ).limit(1)
        )
        if result.scalar() is None:
            return {"code": 1, "msg": "file not found or no permission"}

        # 已有完成记录且非强制 → 直接返回
        if not force:
            result = await db.execute(
                select(UserFileAiDesc).where(
                    UserFileAiDesc.user == user,
                    UserFileAiDesc.md5 == md5_val,
                    UserFileAiDesc.status == 1,
                ).limit(1)
            )
            if result.scalar():
                return {"code": 0, "msg": "already exists"}

        # 全局缓存命中 → 复制到用户表
        result = await db.execute(
            select(FileAiDesc).where(
                FileAiDesc.md5 == md5_val, FileAiDesc.status == 1
            ).limit(1)
        )
        cache = result.scalar()
        if cache and not force:
            return await _copy_cache_to_user(db, user, md5_val, cache)

        # 生成描述
        description = await _generate_description(db, md5_val, filename, file_type, api_key)
        if not description:
            return {"code": 1, "msg": "describe failed"}

        # 向量化
        embedding = await get_embedding(api_key, description)
        vec = np.array(embedding, dtype=np.float32)

        # 写入全局缓存
        await _upsert_file_ai_desc(db, md5_val, description, vec)
        # 写入用户记录
        await _upsert_user_ai_desc(db, user, md5_val, description, vec)
        await db.commit()

        # 追加到 FAISS 索引
        faiss_id = add_vector(user, vec)
        # 更新 faiss_id
        await db.execute(
            f"UPDATE user_file_ai_desc SET faiss_id={faiss_id} "
            f"WHERE user='{user}' AND md5='{md5_val}'"
        )
        await db.commit()

    return {"code": 0, "msg": "ok"}


async def _generate_description(db, md5_val: str, filename: str, file_type: str, api_key: str) -> str:
    """按文件类型生成中文描述：图片 → Qwen-VL，其他 → 文件名+类型"""
    image_types = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}

    if file_type.lower() in image_types:
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val).limit(1)
        )
        fi = result.scalar()
        if fi and fi.url:
            public_url = _build_public_url(fi.url)
            desc = await describe_image(api_key, public_url)
            return desc

    return f"{file_type or '未知'}类型的文件：{filename}"


async def _copy_cache_to_user(db, user: str, md5_val: str, cache) -> dict:
    """从全局缓存 file_ai_desc 复制到用户表 user_file_ai_desc"""
    import numpy as np
    await _upsert_user_ai_desc(db, user, md5_val, cache.description,
                               np.frombuffer(cache.embedding, dtype=np.float32)
                               if cache.embedding else None)
    await db.commit()
    add_vector(user, np.frombuffer(cache.embedding, dtype=np.float32))
    return {"code": 0, "msg": "ok"}


async def _upsert_file_ai_desc(db, md5_val: str, description: str, vec: np.ndarray):
    """写入或更新全局 AI 描述缓存"""
    result = await db.execute(
        select(FileAiDesc).where(FileAiDesc.md5 == md5_val).limit(1)
    )
    existing = result.scalar()
    if existing:
        existing.description = description
        existing.embedding = vec.tobytes()
        existing.model = settings.vl_model
        existing.status = 1
    else:
        db.add(FileAiDesc(
            md5=md5_val, description=description,
            embedding=vec.tobytes(), model=settings.vl_model, status=1,
        ))


async def _upsert_user_ai_desc(db, user: str, md5_val: str, description: str, vec: np.ndarray | None):
    """写入或更新用户 AI 描述记录"""
    result = await db.execute(
        select(UserFileAiDesc).where(
            UserFileAiDesc.user == user, UserFileAiDesc.md5 == md5_val
        ).limit(1)
    )
    existing = result.scalar()
    if existing:
        existing.description = description
        if vec is not None:
            existing.embedding = vec.tobytes()
        existing.status = 1
    else:
        db.add(UserFileAiDesc(
            user=user, md5=md5_val, description=description,
            embedding=vec.tobytes() if vec is not None else None,
            status=1,
        ))


# ============================================================
#  search —— AI 语义搜索
# ============================================================

@router.post("/ai/search")
async def ai_search(body: dict):
    """
    AI 语义搜索 —— 将查询文本向量化后在用户 FAISS 索引中检索，
    按余弦相似度过滤后回查 MySQL 联表返回结果。
    需要: user, token, query, api_key
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    query = body.get("query", "")
    api_key = body.get("api_key", "")
    top_k = body.get("top_k", 10)

    if not query or not api_key:
        return {"code": 1, "msg": "missing query or api_key"}

    if get_ntotal(user) == 0:
        return {"code": 0, "count": 0, "files": []}

    # 查询文本 → 向量 → L2 归一化 → FAISS 搜索
    embedding = await get_embedding(api_key, query)
    query_vec = np.array(embedding, dtype=np.float32)
    results = faiss_search(user, query_vec, top_k)

    # faiss_id → MySQL 联表查询
    async with Session() as db:
        files = []
        for faiss_id, score in results:
            if score < SCORE_THRESHOLD:
                continue

            result = await db.execute(
                select(UserFileAiDesc, UserFileList, FileInfo)
                .join(UserFileList,
                      (UserFileList.user == UserFileAiDesc.user) &
                      (UserFileList.md5 == UserFileAiDesc.md5))
                .join(FileInfo, FileInfo.md5 == UserFileAiDesc.md5)
                .where(
                    UserFileAiDesc.user == user,
                    UserFileAiDesc.faiss_id == faiss_id,
                    UserFileAiDesc.status == 1,
                ).limit(1)
            )
            row = result.first()
            if row:
                uad, ufl, fi = row
                files.append({
                    "md5": uad.md5,
                    "filename": ufl.file_name,
                    "description": uad.description,
                    "url": fi.url,
                    "size": str(fi.size),
                    "type": fi.type,
                    "score": round(score, 4),
                })

        return {"code": 0, "count": len(files), "files": files}


# ============================================================
#  rebuild —— 重建 FAISS 索引
# ============================================================

@router.post("/ai/rebuild")
async def ai_rebuild(body: dict):
    """
    从 MySQL 中读取用户所有已完成的 AI 描述向量，全量重建 FAISS 索引。
    需要: user, token
    """
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    async with Session() as db:
        result = await db.execute(
            select(UserFileAiDesc).where(
                UserFileAiDesc.user == user,
                UserFileAiDesc.status == 1,
                UserFileAiDesc.embedding.isnot(None),
            ).order_by(UserFileAiDesc.id)
        )
        rows = result.scalars().all()

        if not rows:
            return {"code": 0, "msg": "rebuilt", "count": 0}

        vectors = [np.frombuffer(r.embedding, dtype=np.float32) for r in rows]
        rebuild_from_db(user, vectors)

        # 更新 faiss_id（重建后 id 从 0 重新分配）
        idx = load_index(user)
        for i, r in enumerate(rows):
            if i < idx.ntotal:
                r.faiss_id = i
        await db.commit()

    return {"code": 0, "msg": "rebuilt", "count": len(vectors)}
