import secrets
from dataclasses import dataclass

import redis.exceptions
from sqlalchemy import select

from app.database import Session
from app.models import UserInfo
from app.redis_client import redis

# token 键加命名空间前缀，避免用户名与系统键（FILE_PUBLIC_ZSET / chunk:* 等）撞车
TOKEN_KEY_PREFIX = "token:"

ROLE_MEMBER = "member"
ROLE_SUPERVISOR = "supervisor"


@dataclass
class Actor:
    """鉴权通过后的操作者身份：用户名 + 部门 + 角色（部门权限隔离的判权依据）"""
    username: str
    department_id: int
    role: str


async def check_token(user: str, token: str) -> bool:
    """
    验证 Token：从 Redis 读取 user 对应的 token，与传入的 token 比较。
    Redis 瞬断（重启/网络抖动）时返回 False 而非抛 500——
    连接池会在下次调用自动重连，客户端表现为"需重新登录"。
    """
    if not user or not token:
        return False
    try:
        stored = await redis.get(f"{TOKEN_KEY_PREFIX}{user}")
    except redis.exceptions.RedisError:
        return False
    return stored is not None and secrets.compare_digest(stored, token)


async def get_actor(user: str, token: str) -> Actor | None:
    """
    鉴权并解析操作者身份：check_token 通过后查 user_info 取部门与角色。
    一次主键级查询，代价可忽略；角色变更即时生效（无缓存）。
    返回 None 表示未登录/token 失效（调用方统一回 code=4）。
    """
    if not await check_token(user, token):
        return None
    async with Session() as db:
        row = (await db.execute(
            select(UserInfo).where(UserInfo.user_name == user)
        )).scalar()
    if row is None:
        return None
    return Actor(
        username=row.user_name,
        department_id=row.department_id or 1,
        role=row.role or ROLE_MEMBER,
    )


def can_manage_file(actor: Actor, owner: str, department_id: int) -> bool:
    """
    文件管理权限（全系统唯一判权入口）：
    文件所有者本人，或同部门的主管。主管权力边界 = 本部门。
    owner/department_id 取文件行的实际归属，与调用方传参无关。
    """
    if owner == actor.username:
        return True
    return actor.role == ROLE_SUPERVISOR and department_id == actor.department_id
