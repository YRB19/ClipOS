# ClipOS

An Opus Clip clone — upload a long video (or YouTube URL) and ClipOS automatically turns it into short, vertical, captioned clips ready for social media.

![Status: MVP](https://img.shields.io/badge/status-MVP-yellow)

## What it does

1. **Ingest** a video file or YouTube URL
2. **Transcribe** it locally with faster-whisper (base model, CPU, int8)
3. **Detect highlights** with an LLM (NVIDIA NIM, with automatic OpenRouter fallback)
4. **Cut clips** from the source video around each highlight
5. **Reframe** clips to 1080×1920 vertical with dynamic face-tracking panning
6. **Caption** each clip with burned-in ASS subtitles
7. **Score** every clip and auto-generate a hook/title

## Architecture

```
┌──────────────────────────┐       ┌──────────────────────────────┐
│  clipos-frontend (Vite)  │  HTTP │  clipos-backend (FastAPI)    │
│  React 19 + TS + Tailwind├──────►│  app.main:app :8000          │
└──────────────────────────┘       └───────┬──────────────────────┘
                                           │ enqueue RQ jobs
                                           ▼
                                   ┌──────────────┐   ┌──────────────┐
                                   │  Redis (RQ)  │◄──┤  worker.py   │
                                   └──────────────┘   └──────┬───────┘
                                                             │ runs pipeline
                                                             ▼
                                    transcribe → highlights → clips → reframe
                                              → captions → score
```

- **Backend** — FastAPI + SQLModel (SQLite), uvicorn on `:8000`, serves stored files at `/files`
- **Queue** — Redis RQ. The worker (`worker.py`) picks up jobs and runs pipeline stages in sequence
- **Frontend** — React 19 + TypeScript + Vite on `:5173`, Tailwind CSS + shadcn/ui

### Pipeline stages (in order)

| # | Stage | What happens |
|---|-------|--------------|
| 1 | `transcribe` | Extracts 16kHz mono WAV via ffmpeg, transcribes with faster-whisper |
| 2 | `highlights` | LLM picks viral clip candidates from the transcript |
| 3 | `clips` | Cuts each candidate segment from the source with ffmpeg |
| 4 | `reframe` | Dynamic vertical crop (1080×1920) that pans to follow the speaker |
| 5 | `captions` | Generates an ASS subtitle file per clip |
| 6 | `score` | LLM scores and titles each clip |

`burn_captions` is **not** auto-run — it's slow and worth reviewing the generated `.ass` file first. Call it manually per clip.

## Repository structure

```
clipos-backend/          FastAPI backend + pipeline
  app/
    core/                transcribe, highlights, clip_cutter, reframe, captions, scoring, queue
    models/              SQLModel Job model (SQLite)
    routers/             REST endpoints (ingest, jobs, clips, …)
  tests/                 pytest suite
  worker.py              RQ worker entrypoint
  clipos.sh              Dev CLI wrapping the API
  requirements.txt
clipos-frontend/         React + Vite UI
  src/pages/             JobList, Upload, Progress, Gallery, ClipDetail
  src/lib/api.ts         API client
  src/__tests__/         vitest suite
```

## Prerequisites

- **Python 3.11+** (developed on 3.14)
- **Node.js 20+** and npm
- **Redis** (`brew install redis`)
- **ffmpeg** on PATH (`brew install ffmpeg`). For caption burn-in you need an ffmpeg with **libass** — on macOS install `ffmpeg-full` (`brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg-full`). ClipOS prefers `ffmpeg-full` automatically and falls back to plain `ffmpeg`.

## Quick start

Open **three terminals**:

```bash
# Terminal 1 — Redis
redis-server
```

```bash
# Terminal 2 — Backend API
cd clipos-backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in API keys (see below)
uvicorn app.main:app --reload
```

```bash
# Terminal 3 — RQ worker (runs the pipeline)
cd clipos-backend
source .venv/bin/activate
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python worker.py
```

Then start the frontend (Terminal 4, or the same as 2/3):

```bash
cd clipos-frontend
npm install
npm run dev
```

Open **http://localhost:5173**, upload a video, and hit **Run pipeline**. Watch progress on the job's progress page.

> **macOS note:** the `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` prefix on the worker prevents Objective-C runtime crashes when RQ forks after loading `cv2`/`av`/`faster_whisper`.

### API keys (`.env`)

Backend `.env` (in `clipos-backend/`):

```
NVIDIA_API_KEY=            # primary LLM for highlights/scoring (z-ai/glm-5.2 via NIM)
OPENROUTER_API_KEY=        # optional fallback LLM when NVIDIA is unavailable
REDIS_URL=redis://localhost:6379
DATABASE_URL=sqlite:///./clipos.db
```

Without keys, highlight detection, scoring, and auto-titling will fail — transcription and clip cutting work regardless.

Frontend `.env` (in `clipos-frontend/`):

```
VITE_API_BASE_URL=http://localhost:8000
```

## Using the pipeline

### Via the web UI

1. Go to `/upload` and upload an `.mp4` or paste a YouTube URL
2. After ingest, open the job and click **Run pipeline**
3. Poll the progress page (`/jobs/:jobId/progress`) or just wait
4. When done, browse the generated clips in the gallery (`/jobs/:jobId`)

### Via `curl`

```bash
# Upload
curl -X POST http://localhost:8000/ingest/upload -F "file=@video.mp4"

# YouTube
curl -X POST http://localhost:8000/ingest/youtube \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'

# Run the full automatic pipeline (requires status "ingested")
curl -X POST http://localhost:8000/jobs/<job_id>/run

# Poll job state
curl http://localhost:8000/jobs/<job_id>/queue_status
```

### Via the dev CLI

`clipos.sh` wraps the API so you never hand-copy `job_id`/`clip_id` — it remembers your current job in `.clipos_state`.

```bash
cd clipos-backend
./clipos.sh run ~/video.mp4       # upload → transcribe → highlights → clips → score
./clipos.sh full ~/video.mp4      # same + reframe + captions (+ optional burn)
./clipos.sh status                # show current job/clip
./clipos.sh clean all             # flush redis, drop DB, clear storage
```

Full CLI reference is in [`command.md`](command.md).

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Welcome message |
| `GET` | `/health` | Health check → `{"status": "ok"}` |
| `POST` | `/ingest/upload` | Upload an mp4 → `{"job_id": "..."}` |
| `POST` | `/ingest/youtube` | Ingest from YouTube, body `{"url": "..."}` |
| `POST` | `/jobs/{id}/transcribe` | Transcribe job |
| `POST` | `/jobs/{id}/highlights` | Detect clip candidates |
| `POST` | `/jobs/{id}/clips` | Cut clips → `{"clips": [{clip_id, clip_path, duration}]}` |
| `POST` | `/jobs/{id}/clips/{cid}/reframe` | Reframe to 1080×1920 vertical |
| `POST` | `/jobs/{id}/clips/{cid}/captions` | Generate ASS captions |
| `POST` | `/jobs/{id}/clips/{cid}/burn` | Burn captions into the clip (manual, slow) |
| `POST` | `/jobs/{id}/score` | Score + title all clips |
| `POST` | `/jobs/{id}/run` | Enqueue full automatic pipeline (RQ) |
| `GET` | `/jobs` | List jobs, optional `?status=` filter |
| `GET` | `/jobs/{id}` | Job detail with `clip_ids` |
| `GET` | `/jobs/{id}/queue_status` | RQ state: `queued`/`started`/`finished`/`failed` |

Generated files are served from `http://localhost:8000/files/...` (raw videos, clips, transcripts, captions).

## Tests & checks

```bash
# Backend (from clipos-backend)
pytest

# Frontend (from clipos-frontend)
npm test        # vitest
npm run lint    # oxlint
npm run build   # tsc -b && vite build
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Failed to resolve import "@/..."` in the frontend | Restore the `@` alias in `vite.config.ts` (`resolve.alias["@"] = ./src`) |
| Worker crashes on macOS after fork | Prefix with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` |
| Caption burn fails: `libass` missing | Install `ffmpeg-full` (Homebrew) so the ASS filter is available |
| `clipos.sh` says "No current job" | Run `./clipos.sh upload <file>` first, or `./clipos.sh use <job_id>` |
| Stale jobs stuck in `queued`/`failed` | `./clipos.sh clean redis` (or `redis-cli FLUSHALL`), then restart the worker |
| `magic` / RQ errors on restart | Clear stale RQ keys and restart both `uvicorn` and `worker.py` |

## Tech stack

| Layer | Tools |
|-------|-------|
| Backend | FastAPI, uvicorn, SQLModel/SQLite, Redis RQ, yt-dlp, faster-whisper, OpenCV, ffmpeg |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS v3, shadcn/ui, react-router-dom, react-dropzone |
| LLM | NVIDIA NIM (`z-ai/glm-5.2`) with OpenRouter (`z-ai/glm-5.2:free`) fallback |
