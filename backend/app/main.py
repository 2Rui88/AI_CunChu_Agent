from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, files, chunk, share, ai, agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="AI-YunCunChu", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
