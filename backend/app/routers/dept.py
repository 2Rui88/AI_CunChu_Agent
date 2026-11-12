"""
部门文件管理 —— 主管查看本部门全部成员的文件（含私有）

主管权限的操作面：没有本接口，"主管可管理部门内所有文件"就没有入口。
删除不在此实现——统一走 /api/dealfile?cmd=del 并带 owner 参数，
与成员删除自己的文件共用同一判权入口（can_manage_file）。
"""
from fastapi import APIRouter
from sqlalchemy import select, func
from app.database import Session
from app.models import UserFileList, FileInfo, UserInfo
from app.dependencies import get_actor, ROLE_SUPERVISOR
from app.minio_client import presign_from_db_url

router = APIRouter(prefix="/api/dept", tags=["dept"])


@router.post("/files")
async def dept_files(body: dict):
    """本部门全部成员文件列表（主管专属），支持按成员/文件名筛选与分页"""
    user = body.get("user", "")
    token = body.get("token", "")
    actor = await get_actor(user, token)
    if actor is None:
        return {"code": 4}
    if actor.role != ROLE_SUPERVISOR:
        return {"code": 6, "msg": "仅部门主管可访问"}

    member = (body.get("member", "") or "").strip()
    keyword = (body.get("keyword", "") or "").strip()

    try:
        start = max(0, int(body.get("start", 0) or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        count = max(1, min(int(body.get("count", 20) or 20), 500))
    except (TypeError, ValueError):
        count = 20

    async with Session() as db:
        filters = [UserFileList.department_id == actor.department_id]
        if member:
            filters.append(UserFileList.user == member)
        if keyword:
            filters.append(UserFileList.file_name.like(f"%{keyword}%"))

        total = (await db.execute(
            select(func.count()).select_from(UserFileList).where(*filters)
        )).scalar() or 0

        rows = (await db.execute(
            select(UserFileList, FileInfo, UserInfo.nick_name)
            .join(FileInfo, UserFileList.md5 == FileInfo.md5)
            .join(UserInfo, UserInfo.user_name == UserFileList.user)
            .where(*filters)
            .order_by(UserFileList.create_time.desc())
            .offset(start)
            .limit(count)
        )).all()

        # 部门成员清单（筛选下拉用）：部门内有文件的成员
        members = (await db.execute(
            select(UserFileList.user, UserInfo.nick_name)
            .join(UserInfo, UserInfo.user_name == UserFileList.user)
            .where(UserFileList.department_id == actor.department_id)
            .distinct()
            .order_by(UserFileList.user)
        )).all()

        files = [{
            "user": ufl.user,
            "nick_name": nick_name or ufl.user,
            "md5": ufl.md5,
            "file_name": ufl.file_name,
            "size": fi.size,
            "type": fi.type or "",
            "shared_status": ufl.shared_status,
            "url": presign_from_db_url(fi.url) if fi.url else "",
            "create_time": str(ufl.create_time),
        } for ufl, fi, nick_name in rows]

        return {
            "code": 0,
            "total": total,
            "files": files,
            "members": [
                {"username": u, "nick_name": n or u} for u, n in members
            ],
        }
