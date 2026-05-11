import hashlib
import secrets
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from app.database import Session
from app.models import UserInfo
from app.redis_client import redis

router = APIRouter(prefix="/api", tags=["auth"])


class RegRequest(BaseModel):
    username: str
    nick_name: str
    password: str
    phone: str = ""
    email: str = ""


class LoginRequest(BaseModel):
    user: str
    pwd: str


def _make_hash(pwd_md5: str, salt: str) -> str:
    return hashlib.md5((salt + pwd_md5).encode()).hexdigest()


@router.post("/reg")
async def register(body: RegRequest):
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

        salt = secrets.token_hex(8)
        pw_hash = _make_hash(body.password, salt)

        user = UserInfo(
            user_name=body.username,
            nick_name=body.nick_name,
            password=pw_hash,
            salt=salt,
            phone=body.phone,
            email=body.email,
        )
        db.add(user)
        await db.commit()
        return {"code": 0}


@router.post("/login")
async def login(body: LoginRequest):
    async with Session() as db:
        result = await db.execute(
            select(UserInfo).where(UserInfo.user_name == body.user)
        )
        user = result.scalar()
        if user is None:
            return {"code": 1, "msg": "login failed"}

        computed = _make_hash(body.pwd, user.salt)
        if computed != user.password:
            return {"code": 1, "msg": "login failed"}

        token = secrets.token_hex(16)
        await redis.setex(user.user_name, 86400, token)
        return {"code": 0, "token": token}
