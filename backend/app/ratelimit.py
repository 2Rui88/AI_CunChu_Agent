"""
DashScope 全局并发闸门 + 限流退避 —— 共享 API Key 的跨进程保护层

为什么用 Redis 而不是进程内信号量：后端由 Gunicorn 多 worker 部署，
asyncio.Semaphore 只约束单进程，全局并发 = 进程数 × 单进程上限，起不到
保护共享 DashScope Key 的作用。这里用 Redis ZSET 实现跨进程计数信号量：

  acquire: Lua 原子执行「清掉过期成员 → 未满则登记 token」
  release: ZREM 移除自己的 token

成员 score 为到期时间戳（持有时 act = now + lease）：持有者崩溃没 release
也不会永久占坑，最多短暂超卖，对「防打爆限额」这一目标足够。
退避只针对可重试错误（HTTP 429 / 网络抖动）：指数 + 随机抖动；
业务错误（参数/鉴权）不在此重试，由调用方语义决定。

本模块只做流量形状控制，不含任何业务逻辑，也不感知调用目的。
"""
import asyncio
import logging
import random
import time
import uuid

from app.config import settings
from app.redis_client import redis

logger = logging.getLogger("uvicorn.error")

# 闸门名（与 config 的 *_limit 一一对应）
GATE_EMBEDDING = "gate:embedding"
GATE_CHAT = "gate:chat"
GATE_VL = "gate:vl"

_GATE_LIMITS = {}  # 惰性绑定：首次使用时从 settings 读，避免模块导入顺序问题


class RetryableError(Exception):
    """可退避重试的失败：限流（429/Throttling）或网络抖动"""


class GateTimeoutError(Exception):
    """在闸门内排队超过 gate_wait_seconds：调用方应快速失败或走降级"""


def _limit_of(gate: str) -> int:
    if gate not in _GATE_LIMITS:
        _GATE_LIMITS[gate] = {
            GATE_EMBEDDING: settings.gate_embedding_limit,
            GATE_CHAT: settings.gate_chat_limit,
            GATE_VL: settings.gate_vl_limit,
        }.get(gate, 1)
    return _GATE_LIMITS[gate]


# 原子「清理 + 计数 + 登记」；score = 到期时间戳，ARGV: now_ms, limit, expire_ms, token
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local n = redis.call('ZCARD', KEYS[1])
if n < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  return 1
end
return 0
"""


class Gate:
    """跨进程并发闸门（async with 用法；同一协程内不可重入）"""

    def __init__(self, name: str):
        self.name = name
        self.key = f"yuncunchu:{name}"
        self.token = uuid.uuid4().hex

    async def __aenter__(self) -> "Gate":
        limit = _limit_of(self.name)
        deadline = time.monotonic() + settings.gate_wait_seconds
        now_ms = lambda: int(time.time() * 1000)  # noqa: E731
        while True:
            try:
                ok = await redis.eval(
                    _ACQUIRE_LUA, 1, self.key,
                    now_ms(), limit,
                    now_ms() + settings.gate_lease_seconds * 1000,
                    self.token,
                )
            except Exception as exc:  # Redis 不可用：闸门降级为直通，不阻塞业务
                logger.warning("rate gate %s unavailable, pass-through: %s", self.name, exc)
                self.token = None
                return self
            if ok:
                return self
            if time.monotonic() >= deadline:
                raise GateTimeoutError(f"rate gate {self.name} busy (limit={limit})")
            await asyncio.sleep(0.5)

    async def __aexit__(self, *exc_info) -> None:
        if self.token is None:
            return
        try:
            await redis.zrem(self.key, self.token)
        except Exception:
            pass  # 释放失败由租约 TTL 兜底回收


async def call_with_backoff(fn, *, retry_on: tuple = (RetryableError,)):
    """按指数退避重试可重试错误；其余异常原样抛出（调用方语义处理）"""
    attempts = max(1, settings.gate_backoff_attempts)
    base = settings.gate_backoff_base_seconds
    for i in range(attempts):
        try:
            return await fn()
        except retry_on:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base * (2 ** i) + random.uniform(0, 0.3))
