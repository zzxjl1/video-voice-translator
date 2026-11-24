"""FastAPI 入口。"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import commands  # noqa: F401  - 导入即注册全部命令
from app import config
from app.auth import router as auth_router
from app.billing import router as billing_router
from app.auth.db import cleanup_expired, init_db
from app.bus import bus
from app.routers import meta, models, projects, subtitles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,       # 覆盖 uvicorn 已经装好的日志配置
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 线程池里的命令/任务要回到事件循环线程去投递事件，得知道循环是哪个。
    bus.bind(asyncio.get_running_loop())
    init_db()
    cleanup_expired()
    logger.info("Auth DB ready: %s", config.AUTH_DB_PATH)

    async def _cleanup_loop():
        # 每小时清一次过期会话和 OAuth state。只在启动时清的话，长期运行的进程里 sessions 表只增不减。
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.get_running_loop().run_in_executor(None, cleanup_expired)
            except Exception:  # noqa: BLE001 - 清理失败不该影响服务
                logger.warning("Auth cleanup failed", exc_info=True)

    cleanup_task = asyncio.create_task(_cleanup_loop())

    parts = [
        f"{name}={'available' if info['available'] else 'unavailable (' + info['reason'] + ')'}"
        for name, info in config.separation_capabilities().items()
    ]
    logger.info("Vocal separation backends: %s (default mode: %s)", ", ".join(parts), config.SEPARATION_MODE)
    logger.info("Commands registered: %d", len(commands.registry.all_commands()))
    yield
    cleanup_task.cancel()
    logger.info("Shutting down.")


app = FastAPI(
    title="Video Voice Translator API",
    description="Chat-driven video dubbing: one project document, one command set shared by the UI and the AI.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    # 通配来源与 credentials 不能同时成立（浏览器会拒绝）。因此生产/开发
    # 都走同源 /api 反向代理；如果确实需要跨源，CORS_ORIGINS 必须写具体来源。
    allow_credentials="*" not in config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


@api.get("/health")
async def health():
    return {
        "status": "ok",
        "asr_model": config.ASR_MODEL,
        "tts_model": config.TTS_MODEL,
        "llm_model": config.LLM_MODEL,
        "omni_model": config.OMNI_MODEL,
        "separation_mode_default": config.SEPARATION_MODE,
        "separation_backends": config.separation_capabilities(),
    }


# upload 没有 {video_id}，所以在带校验依赖的 router 之外
api.include_router(projects.upload_router)
api.include_router(projects.router)
api.include_router(auth_router.router)
api.include_router(billing_router.router)
api.include_router(meta.router)
api.include_router(models.router)
api.include_router(subtitles.router)

app.include_router(api)
