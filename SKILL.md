---
name: clipos-conventions
description: Project conventions for ClipOS, an Opus-Clip-style tool that turns long videos into auto-generated short vertical clips with captions, using NVIDIA NIM APIs. Consult this skill for ANY work on the ClipOS backend, frontend, video pipeline, transcription, highlight detection, reframing, or caption generation — before writing new modules, before naming new DB fields or JSON keys, and before calling any NVIDIA NIM endpoint. This is the source of truth for schemas, file layout, and API call patterns; check it even if the task seems small, since drift here breaks later pipeline phases.
---

# ClipOS Conventions

ClipOS is built phase-by-phase (see project-plan doc held by the human/architect Claude). This skill exists so every OpenCode session in this repo uses the same schemas, paths, and call patterns — do not invent new ones without flagging it.

## Repo layout (do not restructure without explicit instruction)

```
clipos-backend/
  app/
    main.py
    config.py              # pydantic-settings, reads .env
    routers/                # one file per resource (health, ingest, transcribe, highlights, clips, captions)
    core/                   # business logic, no FastAPI imports here
      ffprobe.py
      transcribe.py
      highlights.py
      reframe.py
      captions.py
    models/                 # SQLModel table definitions
  storage/
    raw/{job_id}/source.mp4
    raw/{job_id}/transcript.json
    raw/{job_id}/highlights.json
    clips/{job_id}/{clip_id}/clip.mp4
    clips/{job_id}/{clip_id}/clip_vertical.mp4
    clips/{job_id}/{clip_id}/captions.ass
  requirements.txt
  .env.example
```

## Canonical JSON schemas — reuse these exactly, don't redefine

**transcript.json** (output of transcription phase):
```json
[{"word": "hello", "start": 0.42, "end": 0.61}, ...]
```

**highlights.json** (output of highlight-detection phase):
```json
[
  {
    "clip_id": "uuid4-string",
    "start": 12.3,
    "end": 47.8,
    "hook_text": "short punchy line describing the hook",
    "virality_score": 0.0,
    "reasoning": "one sentence on why this segment was picked"
  }
]
```

**Job DB row** (SQLModel table `jobs`):
```
job_id: str (uuid4, primary key)
source_type: str  # "upload" | "youtube"
status: str       # "ingested" | "transcribed" | "highlights_ready" | "clips_ready" | "done" | "failed"
created_at: datetime
video_path: str
duration_seconds: float
```

Do not rename these fields across phases. If a phase needs a new field, add it — don't repurpose an existing one.

## NVIDIA NIM call pattern

All NVIDIA NIM calls go through `app/core/nvidia_client.py` (create this once, reuse everywhere — don't inline `requests.post` calls in individual modules).

```python
import os
import requests

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"

def nvidia_chat_completion(model: str, messages: list[dict], **kwargs) -> dict:
    api_key = os.environ["NVIDIA_API_KEY"]
    resp = requests.post(
        f"{NVIDIA_API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, **kwargs},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()
```

- Never hardcode the API key. Always read from `NVIDIA_API_KEY` env var via `config.py`'s Settings object, not `os.environ` directly in route handlers.
- If a NIM endpoint's exact request/response shape is unconfirmed, write the call with a clear `# TODO: confirm request schema against NVIDIA docs` comment and a mock fallback — don't guess silently and let it fail at runtime without a marker.
- Riva ASR (speech-to-text) uses a different endpoint/protocol (gRPC, not the chat completions REST endpoint) — don't try to force it through `nvidia_chat_completion`. Keep it in its own function in `transcribe.py`.

## ffmpeg conventions

- All ffmpeg calls via `subprocess.run([...], check=True, capture_output=True)`, never `shell=True`, never string-interpolated commands (avoids shell injection from user-supplied filenames/URLs).
- Vertical reframe target: 1080x1920, source typically 1920x1080 — crop filter is computed per-clip from tracked bounding box, not a fixed center crop.
- Captions burned in via `ass` subtitle filter (`-vf "subtitles=captions.ass"`), not `drawtext` (too limited for word-highlight animation).

## Status/error conventions

- Every long-running step updates `jobs.status` before and after — never leave a job silently stuck.
- On failure, set `status = "failed"` and store the error message (add `error_message: str | None` to the job row if not already present) rather than raising an unhandled 500 with no DB trace.

## What NOT to do

- Don't switch job queue libraries (Redis+RQ is the standing decision) without flagging it back to the architect.
- Don't add new top-level dependencies without listing them in `requirements.txt` in the same change.
- Don't rename existing JSON schema fields — breaks every downstream phase silently.
