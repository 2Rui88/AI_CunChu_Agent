"""向量化文档只读校验接口 —— 展示已做 AI 描述/切分/向量化的文档及切块明细"""

from fastapi import APIRouter
from sqlalchemy import case, func, select

from app.database import Session
from app.dependencies import check_token
from app.models import FileInfo, UserFileAiDesc, UserFileList

router = APIRouter(prefix="/api", tags=["vector-docs"])

# 与 ai.py 的降级存根标记保持一致（存根只落文本、不向量化）
STUB_LABEL = "未索引内容"


@router.post("/vector/docs")
async def vector_docs(body: dict):
    """已向量化文档列表（按 md5 聚合，每个文件一行，只读）"""
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    try:
        start = max(0, int(body.get("start", 0) or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        count = max(1, min(int(body.get("count", 20) or 20), 500))
    except (TypeError, ValueError):
        count = 20

    async with Session() as db:
        rows = (await db.execute(
            select(
                UserFileAiDesc.md5,
                func.count(UserFileAiDesc.id).label("chunk_count"),
                func.sum(case((UserFileAiDesc.embedding.isnot(None), 1), else_=0)).label("embedded_count"),
                func.sum(case((UserFileAiDesc.context_label == STUB_LABEL, 1), else_=0)).label("stub_count"),
                func.max(UserFileAiDesc.create_time).label("last_time"),
            )
            .where(UserFileAiDesc.user == user)
            .group_by(UserFileAiDesc.md5)
            .order_by(func.max(UserFileAiDesc.create_time).desc())
            .offset(start)
            .limit(count)
        )).all()

        total = (await db.execute(
            select(func.count()).select_from(
                select(UserFileAiDesc.md5)
                .where(UserFileAiDesc.user == user)
                .group_by(UserFileAiDesc.md5)
                .subquery()
            )
        )).scalar() or 0

        total_vectors = (await db.execute(
            select(func.count()).select_from(UserFileAiDesc).where(
                UserFileAiDesc.user == user,
                UserFileAiDesc.embedding.isnot(None),
            )
        )).scalar() or 0

        docs = []
        md5s = [r.md5 for r in rows]
        if md5s:
            # 同一内容可能以不同文件名存有多行，取最新一条作为展示名
            ufl_rows = (await db.execute(
                select(UserFileList)
                .where(UserFileList.user == user, UserFileList.md5.in_(md5s))
                .order_by(UserFileList.create_time.desc())
            )).scalars().all()
            name_by_md5 = {}
            for ufl in ufl_rows:
                if ufl.md5 not in name_by_md5:
                    name_by_md5[ufl.md5] = ufl.file_name or ""

            fi_rows = (await db.execute(
                select(FileInfo).where(FileInfo.md5.in_(md5s))
            )).scalars().all()
            info_by_md5 = {fi.md5: fi for fi in fi_rows}

            for r in rows:
                fi = info_by_md5.get(r.md5)
                docs.append({
                    "md5": r.md5,
                    "file_name": name_by_md5.get(r.md5, ""),
                    "type": fi.type if fi else "",
                    "size": fi.size if fi else 0,
                    "chunk_count": int(r.chunk_count or 0),
                    "embedded_count": int(r.embedded_count or 0),
                    "stub_count": int(r.stub_count or 0),
                    "last_time": str(r.last_time) if r.last_time else "",
                })

        return {"code": 0, "docs": docs, "total": total, "total_vectors": total_vectors}


@router.post("/vector/chunks")
async def vector_chunks(body: dict):
    """单个文档的全部切分明细（只读，不返回向量本体）"""
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    md5_val = body.get("md5", "")

    async with Session() as db:
        # 归属校验：只能查看自己的文件
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user, UserFileList.md5 == md5_val
            ).order_by(UserFileList.create_time.desc()).limit(1)
        )
        ufl = result.scalar()
        if ufl is None:
            return {"code": 1, "msg": "file not found or no permission"}

        fi = (await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5_val)
        )).scalar()

        rows = (await db.execute(
            select(UserFileAiDesc).where(
                UserFileAiDesc.user == user,
                UserFileAiDesc.md5 == md5_val,
            ).order_by(UserFileAiDesc.chunk_index)
        )).scalars().all()

        chunks = [{
            "chunk_index": c.chunk_index,
            "context_label": c.context_label or "",
            "description": c.description or "",
            "faiss_id": c.faiss_id,
            "has_embedding": c.embedding is not None,
            "char_len": len(c.description or ""),
            "model": c.model or "",
            "status": c.status,
            "create_time": str(c.create_time) if c.create_time else "",
        } for c in rows]

        return {
            "code": 0,
            "file": {
                "md5": md5_val,
                "file_name": ufl.file_name or "",
                "type": fi.type if fi else "",
                "size": fi.size if fi else 0,
            },
            "chunks": chunks,
        }
