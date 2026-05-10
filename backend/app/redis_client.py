import redis.asyncio as aioredis
from app.config import settings

redis = aioredis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    decode_responses=True,
)
