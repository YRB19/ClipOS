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
