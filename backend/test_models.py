"""验证 ORM 模型增删改查"""
import asyncio
from app.database import Session, init_db, close_db
from app.models import UserInfo, FileInfo, UserFileList


async def main():
    await init_db()

    async with Session() as db:
        # CREATE
        user = UserInfo(
            user_name="test_crud",
            nick_name="CRUD测试",
            password="abc123",
            salt="deadbeef",
        )
        db.add(user)
        await db.commit()

        print(f"  CREATE  user: id={user.id}, name={user.user_name}")

        # READ
        from sqlalchemy import select
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == "test_crud")
        )
        found = result.scalar()
        assert found is not None, "READ failed"
        print(f"  READ    user: {found.user_name}, salt={found.salt}")

        # UPDATE
        found.nick_name = "已更新"
        await db.commit()
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == "test_crud")
        )
        updated = result.scalar()
        assert updated.nick_name == "已更新", "UPDATE failed"
        print(f"  UPDATE  nick_name: {updated.nick_name}")

        # DELETE
        await db.delete(found)
        await db.commit()
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == "test_crud")
        )
        assert result.scalar() is None, "DELETE failed"
        print(f"  DELETE  ok")

    await close_db()
    print("\n  第二层验证全部通过")


asyncio.run(main())
