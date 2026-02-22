"""
FastAPI application entry point.
"""
import os

# Fix numba cache issue: set a writable cache dir before any numba/librosa import
_numba_cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".numba_cache")
os.makedirs(_numba_cache, exist_ok=True)
os.environ["NUMBA_CACHE_DIR"] = _numba_cache

import logging
from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import video
from app.services.separation_service import init_separator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # Override any existing logging config (e.g. from uvicorn)
)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Video Voice Translator API",
    description="Backend API for video transcription, translation, and TTS",
    version="1.0.0",
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
    return {"status": "ok"}

# Include the video router into the /api router
# Since video.router already has prefix="/api/videos", 
# we should probably fix it to have prefix="/videos" if we are including it in an /api router.
# Let's check video.py again to be sure.
api_router.include_router(video.router)

app.include_router(api_router)


@app.on_event("startup")
async def startup_event():
    """Pre-load models at startup so first request is fast."""
    logger.info("Running startup tasks...")
    init_separator()
    logger.info("Startup tasks complete.")
