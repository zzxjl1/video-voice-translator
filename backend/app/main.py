"""
FastAPI application entry point.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import video

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

# Routers
app.include_router(video.router)


@app.get("/")
async def root():
    return {"message": "Video Voice Translator API", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}
