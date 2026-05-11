import io
import hashlib
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from sqlalchemy import select
from app.database import Session
from app.models import FileInfo, UserFileList, UserFileCount
from app.dependencies import check_token
from app.minio_client import client, BUCKET, ensure_bucket

router = APIRouter(prefix="/api", tags=["files"])


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
        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5,
                UserFileList.file_name == filename,
            )
        )
        if result.scalar():
            return {"code": 5}

        result = await db.execute(select(FileInfo).where(FileInfo.md5 == md5))
        existing = result.scalar()
        if existing:
            existing.count += 1
            db.add(UserFileList(user=user, md5=md5, file_name=filename))
            await db.commit()
            return {"code": 0}

        return {"code": 1}


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user: str = Form(""),
    md5: str = Form(""),
    size: str = Form("0"),
):
    ensure_bucket()

    content = await file.read()
    file_size = int(size)
    suffix = _get_suffix(file.filename)

    if not md5:
        md5 = hashlib.md5(content).hexdigest()

    async with Session() as db:
        result = await db.execute(select(FileInfo).where(FileInfo.md5 == md5))
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
            object_name = f"{md5[:6]}/{file.filename}"
            client.put_object(BUCKET, object_name, io.BytesIO(content), file_size)
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

        await db.commit()

    return {"code": 0}
