"""
FastAPI application entry point.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import models, subtitles, video

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

    # Nothing to preload: vocal separation runs either in the browser (the
    # server only hands over the ONNX weights) or on 302.AI. There is no local
    # model to warm up, which is exactly the point on a small host.
    parts = []
    for name, info in config.separation_capabilities().items():
        if info["available"]:
            parts.append(f"{name}=available")
        else:
            parts.append(f"{name}=unavailable ({info['reason']})")
    logger.info(
        "Vocal separation backends: %s (default mode: %s)",
        ", ".join(parts),
        config.SEPARATION_MODE,
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
    return {
        "status": "ok",
        "asr_model": config.ASR_MODEL,
        "tts_model": config.TTS_MODEL,
        "llm_model": config.LLM_MODEL,
        "separation_mode_default": config.SEPARATION_MODE,
        # Per-backend availability + the reason an option is unusable, so the
        # UI never has to guess (see GET /api/models/separator for the same
        # data alongside the browser model's parameters).
        "separation_backends": config.separation_capabilities(),
    }


api_router.include_router(video.router)
api_router.include_router(video.tts_router)
api_router.include_router(video.asr_router)
api_router.include_router(models.router)
api_router.include_router(subtitles.router)
api_router.include_router(subtitles.video_router)

app.include_router(api_router)
