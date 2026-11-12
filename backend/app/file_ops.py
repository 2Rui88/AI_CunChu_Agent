"""文件-用户关联写入（files / chunk 共用的竞态安全补建路径）+ 部门归属解析"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import Session
from app.models import FileInfo, UserFileList, UserFileCount, UserInfo


async def get_user_department(db, username: str) -> int:
    """查询用户所属部门（上传/转存写 user_file_list 时带上部门维度）"""
    result = await db.execute(
        select(UserInfo.department_id).where(UserInfo.user_name == username)
    )
    dept_id = result.scalar()
    return dept_id or 1


async def attach_file_to_user(user: str, md5: str, filename: str) -> bool:
    """
    竞态补建：并发上传相同内容时，file_info 唯一约束的"败方"回滚后调用此函数，
    重查 file_info 并为当前用户补建 user_file_list 关联与计数（含部门归属）。
    返回 True 表示关联已落库；False 表示用户已拥有或 file_info 不存在（按已存在处理）。
    """
    async with Session() as db:
        result = await db.execute(
            select(FileInfo).where(FileInfo.md5 == md5).with_for_update()
        )
        fi = result.scalar()
        if fi is None:
            await db.rollback()
            return False
        fi.count += 1

        result = await db.execute(
            select(UserFileList).where(
                UserFileList.user == user,
                UserFileList.md5 == md5,
                UserFileList.file_name == filename,
            )
        )
        if result.scalar():
            await db.rollback()
            return False

        dept_id = await get_user_department(db, user)
        db.add(UserFileList(user=user, md5=md5, file_name=filename, department_id=dept_id))

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
            return True
        except IntegrityError:
            await db.rollback()
            return False
