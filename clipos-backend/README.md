# ClipOS Backend

Opus Clip clone — turns long videos into auto-generated short vertical clips with captions.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your NVIDIA_API_KEY and OPENROUTER_API_KEY (optional fallback)
```

## Run

```bash
uvicorn app.main:app --reload
```

Server starts at `http://localhost:8000`.

## Test

```bash
pytest
```

## API

- `GET /` — welcome message
- `GET /health` — health check, returns `{"status": "ok"}`
- `POST /ingest/upload` — upload an mp4 file, returns `{"job_id": "..."}`
- `POST /ingest/youtube` — download from YouTube, body: `{"url": "..."}`, returns `{"job_id": "..."}`
- `POST /jobs/{job_id}/transcribe` — transcribe via local faster-whisper (Whisper base model), returns `{"job_id", "word_count", "duration_covered", "transcript_path"}`
- `POST /jobs/{job_id}/highlights` — detect viral clip candidates via NVIDIA NIM LLM, returns `{"job_id", "candidate_count", "highlights_path"}`
- `POST /jobs/{job_id}/clips` — cut clips from source video based on highlights, returns `{"job_id", "clips": [{"clip_id", "clip_path", "duration"}]}`
- `POST /jobs/{job_id}/clips/{clip_id}/reframe` — reframe clip to 1080x1920 vertical (dynamic face-tracking panning), returns `{"clip_id", "vertical_path"}`

> **Reframing:** Clips are reframed to 1080x1920 using real dynamic panning based on smoothed per-frame face tracking. Every 5th frame is sampled via Haar cascade face detection, gaps are filled by carrying forward the last known face position, and a moving-average window smooths the center-x sequence across the full clip. A time-varying crop expression is built from piecewise-linear interpolation of the smoothed positions and passed to ffmpeg's crop filter, so the crop window pans smoothly to follow the speaker. If no face is ever detected, a static center crop is used as fallback.

> **Note:** Transcription uses local faster-whisper (base model, CPU, int8) for MVP development. Swap back to NVIDIA Riva NIM if/when credentials are confirmed — the output schema is identical either way so nothing downstream breaks.
>
> **LLM fallback:** Highlight detection uses NVIDIA NIM (`z-ai/glm-5.2`) as the primary LLM. If NVIDIA is unavailable (timeout, connection error, account issues), it automatically falls back to OpenRouter (`z-ai/glm-5.2:free`). Set `OPENROUTER_API_KEY` in `.env` to enable the fallback.

### Ingest examples

Upload a file:
```bash
curl -X POST http://localhost:8000/ingest/upload \
  -F "file=@video.mp4"
```

Download from YouTube:
```bash
curl -X POST http://localhost:8000/ingest/youtube \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```

Transcribe a job:
```bash
curl -X POST http://localhost:8000/jobs/{job_id}/transcribe
```

Detect highlights:
```bash
curl -X POST http://localhost:8000/jobs/{job_id}/highlights
```

Cut clips:
```bash
curl -X POST http://localhost:8000/jobs/{job_id}/clips
```

Reframe a clip to vertical:
```bash
curl -X POST http://localhost:8000/jobs/{job_id}/clips/{clip_id}/reframe
```

## Automatic Pipeline (Phase 9)

The full pipeline can run automatically via Redis RQ instead of calling each endpoint by hand. This requires **three processes** running during dev:

```bash
# Terminal 1 — Redis
redis-server

# Terminal 2 — uvicorn
uvicorn app.main:app --reload

# Terminal 3 — RQ worker
python worker.py
```

Then enqueue the automatic pipeline:
```bash
curl -X POST http://localhost:8000/jobs/{job_id}/run
```

This enqueues `run_pipeline(job_id)` onto Redis RQ. The worker picks it up and calls each stage in sequence: transcribe → highlights → clips → reframe → captions → score. If any stage fails, the job is marked `failed` with an `error_message` and later stages are skipped.

### Automatic pipeline endpoints

- `GET /jobs` — list all jobs (most recent first), optional `?status=` filter
- `GET /jobs/{job_id}` — job detail with `clip_ids` list
- `POST /jobs/{job_id}/run` — enqueue the full automatic pipeline (requires status `ingested`)
- `GET /jobs/{job_id}/queue_status` — poll the RQ job state (`queued`/`started`/`finished`/`failed`)

### Note

`burn_captions` is **not** auto-run by the pipeline — it's slow and you may want to review the generated `captions.ass` before burning in. Call it manually after the pipeline completes:

```bash
curl -X POST http://localhost:8000/jobs/{job_id}/clips/{clip_id}/burn
```

## Reliability update — 16 November 2026

This section supersedes the older automatic-pipeline description above. Restart
both the API and worker after updating. No dependency or global Homebrew change is
required. Use `python worker.py` so scheduled retries have a scheduler.

### Media configuration

All extraction, probing, cutting, reframing, caption burning and yt-dlp merging
use one validated FFmpeg/ffprobe pair. Automatic discovery prefers
`/opt/homebrew/opt/ffmpeg-full/bin` (then the Intel Homebrew equivalent and PATH).
Startup verifies matching versions, ASS/subtitles filters and libx264/AAC encoders.
Set `FFMPEG_PATH` and optional `FFPROBE_PATH` to executable paths to override it.
Legacy `FFMPEG_FULL_PATH` accepts either the ffmpeg executable or its bin directory.
An invalid explicit override fails with an actionable startup error.

### Execution and recovery

`POST /jobs/{job_id}/run` atomically creates or returns the single active run.
`POST /jobs/{job_id}/resume` uses the same path for interrupted/partial/legacy jobs.
Each analysis stage and each individual clip's cut, reframe, captions and burn
is a separate RQ task with a durable ID. Caption burn-in now runs automatically.
The final scoring pass retains the existing metadata format. Manual stage APIs
remain available when no automatic run is active.

Run, stage-attempt and clip-state tables are added by ordered, repeatable
migrations at API/worker startup. Existing job IDs, status vocabulary and media
paths remain compatible. Back up a working library before deploying an update;
verification for this change used a backed-up copy, leaving the original intact.

Resume reuses successful checkpoints only when their recorded input/output
signatures still validate. Untracked legacy artifacts remain accessible but are
recomputed on their first managed run. Source identity currently uses immutable
job media size/mtime; full versioned fingerprints/chunk caching belong to phase 4.
JSON checkpoints and media renders are promoted from temporary files after success.
Changing inputs invalidates dependent outputs. No source media is deleted.

Network timeouts/connection failures, HTTP 429 and HTTP 5xx receive at most three
attempts per unit/run, with 10/20-second delayed retries. Each failed attempt still
raises to RQ. Permanent failures stop global analysis, while clip failures allow
independent clips to finish. A partial batch ends as `completed_with_errors` in
the run and `failed` in the legacy job; its successful clips remain downloadable.
Some existing provider wrappers combine errors into RuntimeError; those failures
remain manually resumable pending the provider policy work in phase 5.

`POST /jobs/{job_id}/cancel` fences queued work immediately and stops running work
at the next unit boundary. Current work may take up to its configured deadline.
Defaults: 900 seconds per media subprocess, 960 seconds per media task, 1800 seconds
per analysis task. Timeouts are bounded and configurable via `.env.example`.
Transcription is still a single unit until phase 4 adds chunks; these deadlines do
not establish multi-hour readiness.

The API reconciles active runs every 15 seconds and on status/start requests,
fetching the persisted RQ ID directly. Lost tasks and expired deadlines become
explicit recoverable failures. Redis outages do not alone imply worker death.
A claim interrupted before dispatch is dispatched on reconciliation; an enqueue
failure releases the claim and fences ambiguous late delivery. RQ records expire
after 24 hours (success) / seven days (failure); SQLite keeps execution history.

`GET /jobs/{job_id}/queue_status` retains the legacy fields and adds run status,
execution stage, completed/total units, per-clip failures, heartbeats and per-attempt
elapsed seconds. Unit totals expand after candidate discovery. The progress page
supports cancel/resume and partial-gallery access. Highlights expose available
video variants, restoring captioned playback/downloads after refresh.

### Validation and baseline timing

Run `.venv/bin/python -m pytest -q` from this directory. Tests use isolated working
directories/databases, forbid external provider HTTP calls, and use a private Unix
socket Redis for the queue integration check (requires `redis-server`). Media
fixtures use the validated binary pair. They cover duplicate start/delivery,
enqueue ambiguity, retries, cancellation, invalid checkpoints, per-clip failure,
real RQ failure/workhorse death, migrations and real short-media operations.

For a representative stream, record run start/finish wall time and per-attempt
`elapsed_seconds`, with source duration and hardware/model settings. Keep import
and transfer timing separate and include them in end-to-end reporting. Compare
outputs against setup/payoff, distinct moments, HUD visibility, caption timing,
audio balance and playable 1080×1920 output. No representative three-hour stream
or 20-minute throughput/quality benchmark was run in this phase.
