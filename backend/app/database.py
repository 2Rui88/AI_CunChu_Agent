from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    pass


# URL.create 对用户名/密码中的特殊字符（@ : / 等）自动转义，
# 避免拼字符串时密码含特殊字符导致连接串解析错误
engine = create_async_engine(
    URL.create(
        "mysql+aiomysql",
        username=settings.mysql_user,
        password=settings.mysql_password,
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_db,
        query={"charset": "utf8mb4"},
    ),
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # 取连接前探活，MySQL 重启后自动丢弃失效连接
)

Session = async_sessionmaker(engine, expire_on_commit=False)

#这个函数用于将python的类和数据库中的表进行映射，创建表结构
#这种映射是通过orm来实现的，orm框架会自动收集所有继承了Base的类，
#并根据这些类的定义来创建数据库表结构
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    await engine.dispose()


async def get_db():
    async with Session() as session:
        yield session
