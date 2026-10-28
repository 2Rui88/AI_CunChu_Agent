from sqlalchemy import (
    Column, BigInteger, Integer, String, Text, JSON, Index,
    TIMESTAMP, SmallInteger, UniqueConstraint, text,
)
from sqlalchemy.dialects.mysql import MEDIUMBLOB, MEDIUMTEXT
from app.database import Base


class UserInfo(Base):
    __tablename__ = "user_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_name = Column(String(32), unique=True, nullable=False)
    nick_name = Column(String(32), unique=True, nullable=False)
    password = Column(String(128), nullable=False)  # bcrypt 哈希 60 字符；旧 MD5 为 32 字符
    salt = Column(String(32), nullable=False)       # 仅旧算法使用，bcrypt 自带盐
    algo = Column(String(16), default="md5", comment="密码算法: md5(旧)/bcrypt")
    phone = Column(String(16), default="")
    email = Column(String(64), default="")
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
    pv = Column(Integer, default=0)
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("md5", "file_name", name="uq_share_md5_filename"),
    )


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


class AgentConversation(Base):
    """Agent 会话元数据（历史会话列表行）。全量消息见 AgentMessage，MySQL 为唯一真相源"""
    __tablename__ = "agent_conversation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conv_key = Column(String(36), unique=True, nullable=False,
                      comment="对外会话标识 uuid4，即前端 conversation_id")
    user = Column(String(32), nullable=False, comment="所属用户名")
    title = Column(String(200), nullable=False, default="新对话")
    message_count = Column(Integer, nullable=False, default=0)
    last_seq = Column(BigInteger, nullable=False, default=0,
                      comment="会话内最后一条消息的 seq，行锁下递增分配")
    deleted_at = Column(TIMESTAMP, nullable=True, comment="软删除时间，NULL 为有效")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    update_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        Index("idx_agent_conv_user", "user", "update_time", "id"),
    )


class AgentMessage(Base):
    """Agent 会话消息（OpenAI 消息结构的持久化形态；system 提示词不落库，组装上下文时注入）"""
    __tablename__ = "agent_message"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conv_id = Column(BigInteger, nullable=False, comment="agent_conversation.id")
    seq = Column(BigInteger, nullable=False, comment="会话内顺序号，从 1 连续递增")
    role = Column(String(20), nullable=False, comment="user / assistant / tool")
    content = Column(MEDIUMTEXT, comment="文本正文；assistant 发起工具调用时可为空")
    tool_calls = Column(JSON, comment="assistant 消息的工具调用列表（OpenAI tool_calls 结构）")
    tool_call_id = Column(String(64), comment="role=tool 时回指的 tool_call id")
    create_time = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("conv_id", "seq", name="uq_agent_msg_conv_seq"),
    )
