import secrets

import redis.exceptions

from app.redis_client import redis

# token 键加命名空间前缀，避免用户名与系统键（FILE_PUBLIC_ZSET / chunk:* 等）撞车
TOKEN_KEY_PREFIX = "token:"


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
