from sqlalchemy import (
    Column, BigInteger, Integer, String, Text,
    TIMESTAMP, SmallInteger, UniqueConstraint, text,
)
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from app.database import Base


class UserInfo(Base):
    __tablename__ = "user_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_name = Column(String(32), unique=True, nullable=False)
    nick_name = Column(String(32), unique=True, nullable=False)
    password = Column(String(32), nullable=False)
    salt = Column(String(32), nullable=False)
    phone = Column(String(16), default="")
    email = Column(String(64), default="")
    api_key = Column(String(256), default="")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))


class FileInfo(Base):
    __tablename__ = "file_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    md5 = Column(String(256), unique=True, nullable=False)
    file_id = Column(String(256), nullable=False)
    url = Column(String(512), nullable=False)
    size = Column(BigInteger, default=0)
    type = Column(String(32), default="")
    count = Column(Integer, default=0)


class UserFileList(Base):
    __tablename__ = "user_file_list"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(String(32), nullable=False)
    md5 = Column(String(256), nullable=False)
    file_name = Column(String(128))
    shared_status = Column(Integer, default=0)
    pv = Column(Integer, default=0)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))


class ShareFileList(Base):
    __tablename__ = "share_file_list"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(String(32), nullable=False)
    md5 = Column(String(256), nullable=False)
    file_name = Column(String(128))
    pv = Column(Integer, default=1)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))


class SharePictureList(Base):
    __tablename__ = "share_picture_list"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(String(32), nullable=False)
    filemd5 = Column(String(256), nullable=False)
    file_name = Column(String(128))
    urlmd5 = Column(String(256), nullable=False)
    key = Column(String(8), nullable=False)
    pv = Column(Integer, default=1)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))


class UserFileCount(Base):
    __tablename__ = "user_file_count"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(String(128), unique=True, nullable=False)
    count = Column(Integer, default=0)


class FileAiDesc(Base):
    __tablename__ = "file_ai_desc"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    md5 = Column(String(256), nullable=False)
    chunk_index = Column(Integer, default=0, comment="切片序号，0 表示未分块")
    description = Column(Text, nullable=False)
    embedding = Column(MEDIUMBLOB)
    faiss_id = Column(Integer, default=-1)
    context_label = Column(String(256), default="", comment="切片上下文标签")
    model = Column(String(64), default="")
    status = Column(SmallInteger, default=0)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("md5", "chunk_index", name="uq_md5_chunk"),
    )


class UserFileAiDesc(Base):
    __tablename__ = "user_file_ai_desc"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user = Column(String(32), nullable=False)
    md5 = Column(String(256), nullable=False)
    chunk_index = Column(Integer, default=0, comment="切片序号，0 表示未分块")
    cache_id = Column(BigInteger)
    description = Column(Text, nullable=False)
    embedding = Column(MEDIUMBLOB)
    faiss_id = Column(Integer, default=-1)
    context_label = Column(String(256), default="", comment="切片上下文标签")
    model = Column(String(64), default="")
    status = Column(SmallInteger, default=0)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("user", "md5", "chunk_index", name="uq_user_md5_chunk"),
    )
