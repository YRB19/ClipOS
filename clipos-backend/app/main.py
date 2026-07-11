from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.models.job import init_db
from app.routers import health, ingest, transcribe, highlights, clips, reframe, captions, scoring


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClipOS Backend", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(transcribe.router)
app.include_router(highlights.router)
app.include_router(clips.router)
app.include_router(reframe.router)
app.include_router(captions.router)
app.include_router(scoring.router)


@app.get("/")
def root():
    return {"message": "ClipOS Backend"}
