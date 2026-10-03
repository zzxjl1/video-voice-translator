"""
FastAPI application entry point.
"""
import os

# Fix numba cache issue: set a writable cache dir before any numba/librosa import
_numba_cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".numba_cache")
os.makedirs(_numba_cache, exist_ok=True)
os.environ["NUMBA_CACHE_DIR"] = _numba_cache

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import models, video

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # Override any existing logging config (e.g. from uvicorn)
)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown tasks."""
    logger.info("Running startup tasks...")

    # Vocal separation is optional and heavy. Never load torch unless the
    # operator explicitly opted in (PRELOAD_SEPARATION=true).
    if config.PRELOAD_SEPARATION:
        from app.services import separation_service

        if separation_service.is_available():
            try:
                separation_service.init_separator()
            except Exception as e:
                logger.error("Failed to preload separation model: %s", e)
        else:
            logger.warning(
                "PRELOAD_SEPARATION=true but audio-separator is not installed; skipping."
            )
    else:
        from app.services import separation_service

        logger.info(
            "Vocal separation: %s (preload=%s, deps=%s)",
            "enabled" if config.ENABLE_BGM_SEPARATION_DEFAULT else "DISABLED",
            config.PRELOAD_SEPARATION,
            "installed" if separation_service.is_available() else "not installed",
        )

    logger.info("Startup tasks complete.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Video Voice Translator API",
    description="Backend API for video transcription, translation, and TTS",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Router to wrap everything under /api
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "Video Voice Translator API", "version": "1.0.0"}


@api_router.get("/health")
async def health():
    from app.services import separation_service

    return {
        "status": "ok",
        "asr_model": config.ASR_MODEL,
        "tts_model": config.TTS_MODEL,
        "llm_model": config.LLM_MODEL,
        "bgm_separation_default": config.ENABLE_BGM_SEPARATION_DEFAULT,
        "bgm_separation_available": separation_service.is_available(),
        "export_enabled": config.EXPORT_ENABLED,
    }


api_router.include_router(video.router)
api_router.include_router(models.router)

app.include_router(api_router)
