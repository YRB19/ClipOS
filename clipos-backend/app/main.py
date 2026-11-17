from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models.job import init_db
from app.routers import health, ingest, transcribe, highlights, clips, reframe, captions, scoring, jobs, edits, storage, selection


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.media import media_diagnostics
    media_diagnostics()
    Path(settings.storage_raw).mkdir(parents=True, exist_ok=True)
    Path(settings.storage_clips).mkdir(parents=True, exist_ok=True)
    init_db()
    import asyncio
    from contextlib import suppress
    from app.core.execution import reconciliation_loop
    reconciler = asyncio.create_task(reconciliation_loop())
    try:
        yield
    finally:
        reconciler.cancel()
        with suppress(asyncio.CancelledError):
            await reconciler


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
app.include_router(edits.router)
app.include_router(storage.router)
app.include_router(selection.router)

# Preserve canonical /files/raw and /files/clips URLs with independently configured roots.
app.mount("/files/raw", StaticFiles(directory=str(Path(settings.storage_raw).resolve()), check_dir=False), name="raw-files")
app.mount("/files/clips", StaticFiles(directory=str(Path(settings.storage_clips).resolve()), check_dir=False), name="clip-files")


@app.get("/")
def root():
    return {"message": "ClipOS Backend"}
