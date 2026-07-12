from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models.job import init_db
from app.routers import health, ingest, transcribe, highlights, clips, reframe, captions, scoring, jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClipOS Backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(transcribe.router)
app.include_router(highlights.router)
app.include_router(clips.router)
app.include_router(reframe.router)
app.include_router(captions.router)
app.include_router(scoring.router)
app.include_router(jobs.router)

# Serve stored files (clips, raw videos, etc.) for the frontend gallery
storage = Path(settings.storage_raw).resolve().parent  # -> storage/
app.mount("/files", StaticFiles(directory=str(storage)), name="files")


@app.get("/")
def root():
    return {"message": "ClipOS Backend"}
