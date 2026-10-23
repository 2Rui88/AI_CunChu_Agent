import hashlib
import secrets
import bcrypt
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.database import Session
from app.models import UserInfo
from app.redis_client import redis

router = APIRouter(prefix="/api", tags=["auth"])

LEGACY_ALGO = "md5"
BCRYPT_ALGO = "bcrypt"


class RegRequest(BaseModel):
    # 用户名限制字符集：字母/数字/下划线/连字符，3-32 位
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    nick_name: str = Field(min_length=1, max_length=32)
    # bcrypt 只取前 72 字节，超长直接拒绝
    password: str = Field(min_length=6, max_length=72)
    phone: str = Field(default="", max_length=16)
    email: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    user: str = Field(min_length=1, max_length=32)
    pwd: str = Field(min_length=1, max_length=72)


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（Nginx 反代场景读 X-Forwarded-For 首段）"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _rate_limit(bucket: str, ident: str, limit: int, window: int) -> bool:
    """固定窗口限流：超限返回 False"""
    key = f"rl:{bucket}:{ident}"
    n = await redis.incr(key)
    if n == 1:
        await redis.expire(key, window)
    return n <= limit


def _make_hash(pwd_md5: str, salt: str) -> str:
    """旧方案（algo=md5）：仅用于存量用户验证与登录时透明升级，新注册不再使用"""
    return hashlib.md5((salt + pwd_md5).encode()).hexdigest()


def _hash_bcrypt(password: str) -> str:
    """新方案：bcrypt 自带盐、带工作因子（rounds=12），抵抗离线暴力破解"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def _check_bcrypt(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


@router.post("/reg")
async def register(body: RegRequest, request: Request):
    # 注册限流：每 IP 每小时 5 次
    if not await _rate_limit("reg", _client_ip(request), 5, 3600):
        return {"code": 7, "msg": "too many registrations"}

    async with Session() as db:
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == body.username)
        )
        if result.scalar():
            return {"code": 2, "msg": "username exists"}

        result = await db.execute(
            select(UserInfo).where(UserInfo.nick_name == body.nick_name)
        )
        if result.scalar():
            return {"code": 6, "msg": "nickname exists"}

        # salt 仅为满足列非空保留；bcrypt 哈希内含盐
        salt = secrets.token_hex(8)
        pw_hash = _hash_bcrypt(body.password)

        user = UserInfo(
            user_name=body.username,
            nick_name=body.nick_name,
            password=pw_hash,
            salt=salt,
            algo=BCRYPT_ALGO,
            phone=body.phone,
            email=body.email,
        )
        db.add(user)
        await db.commit()
        return {"code": 0}


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    # 登录限流：单用户名 5 分钟 10 次 + 单 IP 5 分钟 30 次，挡住暴力试密码
    if not await _rate_limit("login-user", body.user, 10, 300):
        return {"code": 3, "msg": "too many attempts"}
    if not await _rate_limit("login-ip", _client_ip(request), 30, 300):
        return {"code": 3, "msg": "too many attempts"}

    async with Session() as db:
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == body.user)
        )
        user = result.scalar()
        if user is None:
            return {"code": 1, "msg": "login failed"}

        if user.algo == BCRYPT_ALGO:
            ok = _check_bcrypt(body.pwd, user.password)
        else:
            # 常量时间比较，避免逐字节时序侧信道
            ok = secrets.compare_digest(_make_hash(body.pwd, user.salt), user.password)
            if ok:
                # 透明升级：老用户验证通过后立即改存 bcrypt，无需停机迁移
                user.password = _hash_bcrypt(body.pwd)
                user.algo = BCRYPT_ALGO
                await db.commit()

        if not ok:
            return {"code": 1, "msg": "login failed"}

        token = secrets.token_hex(16)
        await redis.setex(f"token:{user.user_name}", 86400, token)
        return {"code": 0, "token": token}
