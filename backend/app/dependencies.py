import secrets

from app.redis_client import redis

# token 键加命名空间前缀，避免用户名与系统键（FILE_PUBLIC_ZSET / chunk:* 等）撞车
TOKEN_KEY_PREFIX = "token:"


async def check_token(user: str, token: str) -> bool:
    """验证 Token：从 Redis 读取 user 对应的 token，与传入的 token 比较。"""
    if not user or not token:
        return False
    stored = await redis.get(f"{TOKEN_KEY_PREFIX}{user}")
    return stored is not None and secrets.compare_digest(stored, token)
