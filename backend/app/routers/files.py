import io
import asyncio
import hashlib
from fastapi import APIRouter, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import Session
from app.models import FileInfo, UserFileList, UserFileCount, UserFileAiDesc
from app.dependencies import check_token
from app.file_ops import attach_file_to_user
from app.minio_client import client, BUCKET, ensure_bucket, presign_from_db_url

router = APIRouter(prefix="/api", tags=["files"])

# 与 nginx client_max_body_size 对齐的直传上限：代理层防线之外，
# Docker 网络内直连 backend:8000 时由此兜底（超限走分片上传）
_MAX_UPLOAD_BODY = 12 * 1024 * 1024


def _get_suffix(filename: str) -> str:
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "unknown"


@router.post("/md5")
async def md5_check(body: dict):
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    md5 = body.get("md5", "")
    filename = body.get("fileName", "")

    async with Session() as db:
        # 先拿 file_info 行锁，把"用户已拥有"检查放进锁内——
        # 检查在锁外的话，同用户并发秒传同一 (md5, filename) 都通过检查，
        # 各插一行重复的 user_file_list（该表无唯一约束）
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5).with_for_update()
        )
        existing = result.scalar()
        if not existing:
            return {"code": 1}

        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5,
                UserFileList.file_name == filename,
            )
        )
        if result.scalar():
            return {"code": 5}

        existing.count += 1
        db.add(UserFileList(user=user, md5=md5, file_name=filename))
        # 秒传同样计入用户文件数（与上传/删除路径对齐，否则计数漂移）
        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == user)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=user, count=1))
        try:
            await db.commit()
        except IntegrityError:
            # 并发秒传触发唯一约束（如 user_file_count.user）：
            # 回滚后补建本用户关联，避免"败方"文件凭空消失
            await db.rollback()
            return {"code": 0 if await attach_file_to_user(user, md5, filename) else 5}
        return {"code": 0}


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user: str = Form(""),
    token: str = Form(""),
    md5: str = Form(""),
):
    if not await check_token(user, token):
        return {"code": 4}

    await asyncio.to_thread(ensure_bucket)

    # 流式分块读取并同步计算 MD5：超过上限即中止，不再整文件读入内存
    # （防御纵深：nginx client_max_body_size 只在代理层生效）
    hasher = hashlib.md5()
    buf = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        hasher.update(block)
        buf.extend(block)
        if len(buf) > _MAX_UPLOAD_BODY:
            return {"code": 1, "msg": "文件过大，请使用分片上传"}
    content = bytes(buf)
    file_size = len(content)
    suffix = _get_suffix(file.filename)

    # 服务端计算 MD5，客户端上报值仅作比对，防止伪造 md5 覆盖他人文件
    actual_md5 = hasher.hexdigest()
    if md5 and md5.lower() != actual_md5:
        return {"code": 1, "msg": "md5 mismatch"}
    md5 = actual_md5

    async with Session() as db:
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5).with_for_update()
        )
        existing = result.scalar()

        if existing:
            existing.count += 1
            file_id = existing.file_id
            url = existing.url

            result = await db.execute(
                select(UserFileList).where(
                    UserFileList.user == user,
                    UserFileList.md5 == md5,
                    UserFileList.file_name == file.filename,
                )
            )
            if result.scalar():
                return {"code": 5}
        else:
            # 对象名使用完整 md5：同内容才可能同名，避免 md5[:6] 前缀碰撞覆盖他人文件
            object_name = f"{md5}/{file.filename}"
            await asyncio.to_thread(
                client.put_object, BUCKET, object_name, io.BytesIO(content), file_size
            )
            file_id = object_name
            url = f"/files/{BUCKET}/{object_name}"

            db.add(FileInfo(
                md5=md5,
                file_id=file_id,
                url=url,
                size=file_size,
                type=suffix,
                count=1,
            ))

        db.add(UserFileList(user=user, md5=md5, file_name=file.filename))

        result = await db.execute(
            select(UserFileCount).where(UserFileCount.user == user)
        )
        ufc = result.scalar()
        if ufc:
            ufc.count += 1
        else:
            db.add(UserFileCount(user=user, count=1))

        try:
            await db.commit()
        except IntegrityError:
            # 并发上传相同内容：另一请求已抢先创建 file_info。
            # 回滚后补建本用户关联与计数（对象存储中同内容对象幂等），而非直接报"已存在"
            await db.rollback()
            if await attach_file_to_user(user, md5, file.filename):
                return {"code": 0}
            return {"code": 5}

    return {"code": 0}


@router.post("/myfiles")
async def myfiles(body: dict):
    """用户文件列表查询"""
    user = body.get("user", "")
    token = body.get("token", "")
    if not await check_token(user, token):
        return {"code": 4}

    cmd = body.get("cmd", "normal")
    if cmd != "normal":
        return {"code": 1}

    # 分页参数钳制，防异常值打穿查询
    try:
        start = max(0, int(body.get("start", 0) or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        count = max(1, min(int(body.get("count", 20) or 20), 500))
    except (TypeError, ValueError):
        count = 20

    async with Session() as db:
        result = await db.execute(
            select(UserFileList, FileInfo)
            .join(FileInfo, UserFileList.md5 == FileInfo.md5)
            .where(UserFileList.user == user)
            .order_by(UserFileList.create_time.desc())
            .offset(start)
            .limit(count)
        )
        rows = result.all()

        # 已建立内容索引的 md5 集合（排除"未索引内容"存根），供前端显示重建入口
        indexed: set = set()
        if rows:
            result = await db.execute(
                select(UserFileAiDesc.md5).where(
                    UserFileAiDesc.user == user,
                    UserFileAiDesc.status == 1,
                    UserFileAiDesc.md5.in_([ufl.md5 for ufl, _ in rows]),
                ).distinct()
            )
            desc_md5s = {r[0] for r in result.all()}
            if desc_md5s:
                stub = await db.execute(
                    select(UserFileAiDesc.md5).where(
                        UserFileAiDesc.user == user,
                        UserFileAiDesc.md5.in_(desc_md5s),
                        UserFileAiDesc.context_label == "未索引内容",
                    ).distinct()
                )
                indexed = desc_md5s - {r[0] for r in stub.all()}

        files = []
        for ufl, fi in rows:
            files.append({
                "md5": ufl.md5,
                "file_name": ufl.file_name,
                "url": presign_from_db_url(fi.url) if fi.url else "",
                "size": fi.size,
                "type": fi.type,
                "pv": ufl.pv,
                "shared_status": ufl.shared_status,
                "content_indexed": ufl.md5 in indexed,
                "create_time": str(ufl.create_time),
            })
        return {"code": 0, "files": files}
