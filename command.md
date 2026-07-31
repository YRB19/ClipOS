# ClipOS — Command Reference

All commands you need to set up, run, test, and debug ClipOS. Commands marked `[backend]` / `[frontend]` run inside that subdirectory.

## One-time setup

```bash
# Prerequisites (macOS / Homebrew)
brew install redis
brew install ffmpeg
# Optional: ffmpeg with libass for caption burn-in
brew tap homebrew-ffmpeg/ffmpeg
brew install homebrew-ffmpeg/ffmpeg/ffmpeg-full

# Backend environment
cd clipos-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: add NVIDIA_API_KEY (and OPENROUTER_API_KEY for fallback)

# Frontend dependencies
cd clipos-frontend
npm install
```

## Running the app (4 terminals)

```bash
# 1) Redis
redis-server

# 2) Backend API
cd clipos-backend && source .venv/bin/activate
uvicorn app.main:app --reload          # → http://localhost:8000

# 3) RQ worker (runs the pipeline)
cd clipos-backend && source .venv/bin/activate
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python worker.py

# 4) Frontend
cd clipos-frontend
npm run dev                             # → http://localhost:5173
```

> macOS: the `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` prefix on the worker prevents Objective-C runtime crashes after RQ forks.

## Frontend

```bash
npm run dev        # start dev server (:5173)
npm run build      # typecheck (tsc -b) + production build
npm run preview    # preview the production build
npm run test       # run vitest suite
npm run lint       # run oxlint
```

## Backend

```bash
uvicorn app.main:app --reload    # dev server (:8000)
pytest                           # run all tests
pytest tests/test_health.py -v   # run a single test file
```

## Manual pipeline (curl)

```bash
BASE=http://localhost:8000

# Ingest
curl -X POST $BASE/ingest/upload -F "file=@video.mp4"
curl -X POST $BASE/ingest/youtube -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'

# Automatic pipeline (single RQ job)
curl -X POST $BASE/jobs/JOB_ID/run

# Step-by-step (manual stages)
curl -X POST $BASE/jobs/JOB_ID/transcribe
curl -X POST $BASE/jobs/JOB_ID/highlights
curl -X POST $BASE/jobs/JOB_ID/clips
curl -X POST $BASE/jobs/JOB_ID/clips/CLIP_ID/reframe
curl -X POST $BASE/jobs/JOB_ID/clips/CLIP_ID/captions
curl -X POST $BASE/jobs/JOB_ID/clips/CLIP_ID/burn
curl -X POST $BASE/jobs/JOB_ID/score

# Inspect
curl $BASE/health
curl $BASE/jobs
curl $BASE/jobs?status=ingested
curl $BASE/jobs/JOB_ID
curl $BASE/jobs/JOB_ID/queue_status
```

## Dev CLI (`clipos.sh`)

Runs from `clipos-backend/` (or anywhere with `CLIPOS_BASE_URL` set). Remembers your current job/clip in `.clipos_state`.

```bash
./clipos.sh upload <file>              # upload; sets current job
./clipos.sh run <file>                 # upload → transcribe → highlights → clips → score
./clipos.sh full <file>                # run + reframe + captions (+ optional burn)
./clipos.sh transcribe [job_id]
./clipos.sh highlights [job_id]
./clipos.sh clips [job_id]
./clipos.sh reframe [clip_id] [job_id]
./clipos.sh captions [clip_id] [job_id]
./clipos.sh burn [clip_id] [job_id]
./clipos.sh score [job_id]
./clipos.sh status [job_id]            # show current job/clip + base URL
./clipos.sh jobs                       # list recent jobs
./clipos.sh use <job_id>               # manually set current job
./clipos.sh clean redis                # flush stale RQ keys
./clipos.sh clean db                   # drop SQLite DB (recreates empty)
./clipos.sh clean storage              # delete raw/ + clips/ files
./clipos.sh clean all                  # redis + db + storage
```

## Redis / RQ

```bash
redis-server                    # start Redis
redis-cli ping                  # → PONG
redis-cli KEYS 'rq:job:*'       # list RQ jobs
redis-cli FLUSHALL              # hard reset (or: ./clipos.sh clean redis)
redis-cli LLEN rq:queue:default # jobs waiting in the default queue
```

## LLM providers

| Provider | Env var | Model |
|----------|---------|-------|
| NVIDIA NIM (primary) | `NVIDIA_API_KEY` | `z-ai/glm-5.2` |
| OpenRouter (fallback) | `OPENROUTER_API_KEY` | `z-ai/glm-5.2:free` |

ClipOS tries NVIDIA first, then falls back to OpenRouter automatically.

## Useful paths

```bash
clipos-backend/clipos.db         # SQLite job store
clipos-backend/storage/raw/      # uploaded source videos
clipos-backend/storage/clips/    # generated clips (by job_id/clip_id)
clipos-backend/.clipos_state     # CLI state (job_id, clip_id)
clipos-backend/.env              # backend env (API keys, URLs)
clipos-frontend/.env             # VITE_API_BASE_URL
```

## Common fixes

```bash
# Frontend import errors ("Failed to resolve import @/...")
# → make sure vite.config.ts has: resolve.alias { "@": "./src" }

# Worker crashed / jobs stuck
./clipos.sh clean redis
# then restart uvicorn + worker

# "No current job" from clipos.sh
./clipos.sh use <job_id>   # or ./clipos.sh upload <file> first
```
