import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import auth, files, chunk, share, ai, agent
from app.database import init_db
from app.minio_client import ensure_bucket

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表和存储桶存在（create_all / bucket_exists 均为幂等操作）
    try:
        await init_db()
        ensure_bucket()
    except Exception as exc:
        logger.error("startup init failed: %s", exc)
        raise
    yield


app = FastAPI(title="AI-YunCunChu", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # 收敛为白名单（生产同源部署不需要跨域；认证走 body 内 token，无 cookie 依赖）
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(chunk.router)
app.include_router(share.router)
app.include_router(ai.router)
app.include_router(agent.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
