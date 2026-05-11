from app.redis_client import redis


async def check_token(user: str, token: str) -> bool:
    """验证 Token：从 Redis 读取 user 对应的 token，与传入的 token 比较。"""
    if not user or not token:
        return False
    stored = await redis.get(user)
    return stored is not None and stored == token
