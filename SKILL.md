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

## Phase 1 execution additions — 2026-11-16

The implementation plan in `docs/2026-11-16-implementation-plan.md` now governs
phase order (1 → 2 → 4 → 5 → 6). Automatic caption burn is part of the normal
pipeline. Manual stage endpoints are optional overrides and reject changes while
an automatic run is active.

- Resolve every FFmpeg/ffprobe call through `core/media.py`. `FFMPEG_PATH` and
  `FFPROBE_PATH` mean executable paths. Legacy `FFMPEG_FULL_PATH` also accepts a bin
  directory. Do not add versioned Cellar paths to individual modules.
- `app/migrations/` holds ordered additive migrations. `models/execution.py` adds
  `pipeline_runs`, `stage_attempts` and `clip_states`; canonical `jobs` fields and
  transcript/highlight keys remain unchanged. `PipelineRun.active_key` is a unique,
  nullable job ID and is the database claim for one active run per job.
- `core/pipeline.py:run_stage` executes one bounded RQ unit, records its input/output
  checkpoint and elapsed time, and raises failures to RQ. `core/execution.py`
  reconciles lost tasks and exposes detailed state. Persist task IDs before enqueue;
  never discover a job by scanning all queue registries.
- Detailed run states are additive: queued/running/retrying/cancelling/completed/
  completed_with_errors/failed/cancelled. Keep the legacy job status vocabulary.
  During execution use run/stage state for progress; terminal partial/cancelled
  runs map to legacy `failed` with an explanatory message.
- `POST /jobs/{id}/run` and `/resume` are idempotent for active runs. Cancellation
  happens at safe unit boundaries. Reuse only successful, validated checkpoints;
  never mark a file complete merely because it exists. Use temporary files and
  atomic promotion for checkpoint/render artifacts.
- Start workers with `python worker.py` (scheduler enabled for delayed retries).
  RQ remains pinned at 2.0.0; worker spawning/upgrade measurements remain phase 6.

## Phase 2 recipe/render additions — 2026-11-16

- Migration 3 adds `source_assets`, `clip_edits`, immutable `edit_revisions` and
  durable `render_outputs` in `models/edit.py`. AI candidate metadata stays in
  canonical highlights.json; edits must never overwrite that selection data.
- `core/recipes.py` defines the strict version-1 edit schema. Source references
  contain job ID and SHA-256 fingerprint, never arbitrary filesystem paths.
  Source intervals use absolute seconds. Caption words and crop keyframes use
  output-local seconds. All source regions use normalized [0,1] coordinates in
  displayed source space, including sample-aspect-ratio correction.
- Edit saves require `expected_revision` and use an atomic compare/update. Keep
  historical revisions immutable; conflict responses are HTTP 409. Automatic
  retries preserve existing edits instead of regenerating them.
- `core/assets.py` fingerprints/probes original media and builds versioned proxy,
  thumbnail and waveform artifacts. Validate their signatures before reuse. The
  original stays unchanged. Size/mtime allow reuse of an already registered hash.
- The normal pipeline now runs assets → transcription → highlights → per-clip
  recipe/direct source render → scoring. Keep old cut/reframe/caption/burn task
  handlers and manual endpoints for compatibility; they are not final-render
  dependencies. The default gaming layout fits the entire gameplay on a solid
  background; do not infer an unverified facecam rectangle.
- `core/renderer.py` performs trims, layout, crop keyframes, ASS and selected-track
  audio mixing in one final encode from original media. Preview uses the same
  semantics at 360×640; final is 1080×1920 H.264/AAC, yuv420p, rational 1–60 fps.
  Source-space facecam regions must be provided for gameplay_facecam composition.
- `core/renders.py` pins RQ requests to immutable revision IDs, deduplicates active
  or valid completed work, records errors/heartbeats and reconciles lost tasks.
  Outputs live under `clips/{job}/{clip}/renders/{render_id}/{preview|final}.mp4`;
  preserve earlier renders and legacy files. Expose availability/current-revision
  state from the backend, never solely from browser state.
- Render to temporary files, verify geometry, duration, frame rate, pixel/sample
  aspect format and A/V duration alignment, then promote atomically. Version 1
  rejects HDR and rotated final inputs until their deliberate conversion presets
  are implemented; do not silently mislabel those outputs as supported SDR.

## Phase 4 long-source additions — 2026-11-16

- Migrations 4/5 add `source_imports` (durable remote URL/import state and optional
  ASR audio-stream override) and nullable `clip_edits.generated_signature`.
  `jobs.source_type` additionally supports `local`. Preserve original containers,
  stream indexes and codecs; use `Job.video_path`, not a hardcoded source.mp4.
- Automatic work is download (remote only) → bounded navigation sample → repeated
  transcribe_chunk/highlights_chunk → early per-candidate recipe/render → final
  scoring. Chunk indexes use the existing attempt clip_id slot only for those two
  stage names; never create ClipState records or apply per-clip failure isolation
  to analysis chunks. A chunk failure stops the run and successful chunks survive.
- `core/chunks.py` owns chunk windows, identities, atomic JSON/digest checkpoints,
  absolute word offsets, overlap deduplication, incremental canonical aggregates
  and candidate count/duration budgets. Empty transcript/selection arrays can be
  valid successful checkpoints. Dense speech splits into bounded prompts without
  dropping words. ASR uses VAD with the adapter's restored original timestamps.
- Prefer a track titled microphone/mic/voice/commentary for ASR; allow an explicit
  global audio stream index. Pad delayed tracks before ASR, preserving source
  silence. Keep source tracks distinct; final recipe audio selection remains
  separate from the ASR choice. For explicitly named game/desktop plus microphone
  tracks, default exports mix game at 0.5 gain with microphone at 1.0; unnamed or
  composite sources retain their first-track mix.
- Silent chunks receive explicitly unassessed, zero-score visual candidates.
  This is eligibility, not evidence of a strong moment; visual assessment stays
  in Phase 5. Do not infer clip quality from an empty transcript.
- Cache keys include source fingerprint, track, window, ASR model/VAD/version;
  selection adds transcript digest, prompt/providers/version and budgets. Recipe
  validity uses only transcript words overlapping that clip. Automatically created
  edits may refresh changed inputs; any explicit save clears the automatic marker
  and protects human edits. Pre-migration revisions are also protected.
- Local import resolves inside configured MEDIA_ROOT and rejects symlink escapes.
  Remote requests persist a job/run before enqueue; downloads have bounded time,
  playlist and source-size limits. Failed imports remain visible/resumable.
- Navigation proxy/waveform coverage is explicitly bounded (default first 60s),
  with source duration recorded separately. Do not present it as a full timeline
  proxy. Browser-compatible previews are derived without changing the original.
- `GET /storage/cache` plans cleanup; `POST /storage/cache/cleanup` explicitly
  removes only inactive raw chunks/navigation assets under SQLite's write lock.
  Never delete originals, canonical aggregates, edit history or render outputs.
  CACHE_MAX_GB stops further cache work when exceeded (one bounded unit may cross
  the limit); it is not a quota on originals/final exports. No automatic eviction.
- Gallery and early clip details poll as work continues. An unrendered candidate
  shows its hook/reason/status without a video/download URL. Manual review remains
  optional. Real multi-hour inference/quality and the 20-minute target remain
  separate validation milestones; synthetic scheduling is not a benchmark.
