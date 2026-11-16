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

## Phase 2 — source-based recipes and renders (16 November 2026)

This supersedes Phase 1's automatic cut/reframe/burn chain. Restart API and worker
to apply migration 3 and use the new automatic work units. Existing manual stage
APIs and files remain available. No dependencies or global packages changed.

The automatic path prepares navigation assets, transcribes/selects candidates,
creates a default recipe for each clip, renders directly from the original, then
scores/titles the batch. Final video no longer needs cut or reframed intermediate
encodes. Default layout preserves the full gameplay on a solid background. A
provided source-space facecam rectangle enables the gameplay/facecam composition;
manual crops support time-varying x/y keyframes. Facecam discovery is not added in
this phase. Layout/caption quality on actual gaming streams remains to be evaluated.

Migration 3 adds source assets, edit heads, immutable revisions and render outputs.
Original sources receive a SHA-256 fingerprint and expanded probe metadata with
rational frame rates, sample aspect ratio, color fields and individual audio tracks.
A cached source hash is reused while size/mtime remain unchanged. Proxy MP4, JPEG
thumbnail and bounded waveform peaks are stored under
`raw/{job_id}/assets/{fingerprint}/navigation-v1/`. They are reusable navigation
assets; final and clip-preview rendering both read original media. Full-stream
proxy preparation is still a bounded whole-source task; chunking and profiling
remain phases 4 and 6.

### Additive APIs

| Method/path | Behavior |
| --- | --- |
| `GET /jobs/{job}/assets` | Probe metadata and available navigation-artifact URLs; pending until source registration/preparation. |
| `GET /jobs/{job}/clips/{clip}/edit` | Load the current recipe; initialize revision 1 from the candidate if absent. |
| `PUT /jobs/{job}/clips/{clip}/edit` | Save `{expected_revision, recipe}`; return HTTP 409 on a stale revision. |
| `POST /jobs/{job}/clips/{clip}/renders` | Queue `{revision_id, kind: "preview" \| "final"}`; return durable render state with HTTP 202. |
| `GET /jobs/{job}/clips/{clip}/renders` | Render history, errors, availability, output URLs and `is_current` flags. |

The recipe JSON schema is defined by `EditRecipe` in `app/core/recipes.py` and
exposed through FastAPI's OpenAPI schema. It includes schema_version=1, a source
job/fingerprint reference, one or more absolute source intervals, layout/source
regions, crop keyframes, caption text/style, selected audio stream indexes/gains
and export settings. Region coordinates are normalized [0,1] in displayed source
space. Caption/keyframe times are seconds on the concatenated output timeline;
caption timing edits accompanying trim changes must update those local times.
A recipe supports up to 16 intervals and 300 seconds of output. Saves never rerun
ASR/selection. The optional full editing UI remains phase 3.

Render requests are pinned to their requested revision, including requests that
finish after a newer edit is saved. Repeated active or valid completed requests
reuse a render; failed/missing outputs can be requested again. Each result uses a
new immutable path under `clips/{job}/{clip}/renders/{render_id}/` and has a paired
ASS file. Worker failures raise to RQ and persist to SQLite. Lost/expired tasks are
reconciled every 15 seconds; a failed enqueue fences ambiguous late delivery.
Preview and final render state persist across browser refreshes. Highlights now
include `final_render`, and gallery/player/download views prefer the current final
output. Older render revisions remain accessible through render history; legacy
variants remain available at their original paths. Starting automatic work is
blocked while a standalone render is active; recipe/render override requests are
blocked while an automatic pipeline is active.

Both preview (360×640) and final (1080×1920) share the same composition/caption/audio
semantics. Final encoding is one libx264/AAC MP4 pass with faststart, yuv420p and an
explicit rational frame rate (defaults to the source rate up to 60 fps). Audio
selections use ffprobe's global stream indexes. Selected tracks are resampled,
mixed with explicit gain and limited; an empty selection produces a silent AAC
track. Delayed source audio retains its offsets. Captions are output-local ASS;
control braces/backslashes in literal text display as full-width punctuation to
prevent subtitle override injection. Word grouping/styling improvements remain
phase 3. No HDR tone mapping or rotation normalization is implemented yet: those
final inputs fail explicitly pending the planned presets.

Temporary output is probed for codec, dimensions, duration, frame rate, pixel and
sample aspect format, audio presence and A/V duration alignment before promotion.
A changed source cannot publish a result under its previous identity. `/files/raw`
and `/files/clips` serve their configured roots independently, preserving existing
media URLs and supporting byte-range playback of the new immutable outputs.

### Phase 4: long recordings and incremental results

Automatic jobs now use five-minute ASR/selection sections with 15-second context
on each side. Each section is a separately timed RQ task with an atomic, validated
checkpoint. Cancel stops at a unit boundary; `/resume` reuses completed sections.
Transcript words remain in absolute source seconds. VAD and delayed-track padding
preserve silence offsets; overlaps are deduplicated. Empty speech is a valid
checkpoint, with explicitly unassessed visual candidates left eligible for Phase 5.
Dense speech is split into overlapping, bounded text prompts. Defaults cap the
current selection at 10 candidates, 15–90 seconds each and 600 seconds total.
Selection can change as later sections arrive; earlier render history is retained.
This phase does not establish candidate quality or real-stream throughput.

Uploads accept MP4/MKV/MOV/WebM/M4V and preserve their containers. Set `MEDIA_ROOT`
in `.env` to enable the **Local library** upload tab. Imports reference the original
file; keep it available at that path. Files and symlinks must resolve inside the
configured root. The local form probes tracks and lets you choose a microphone;
automatic ASR prefers mic/voice/commentary track titles, then the first audio track.
An explicit choice is also available through `PUT /jobs/{id}/audio-track` while
idle. Final mixes stay separate: explicitly named game/desktop plus mic tracks
mix at 0.5/1.0 gain; other sources retain the first-track mix. Edit recipes can
select individual source tracks and gains without modifying the originals.

`POST /ingest/youtube` returns HTTP 202 and a durable job ID immediately. It queues
the download and subsequent pipeline; callers can navigate straight to progress.
Repeated `/run` calls remain idempotent. Download errors, dispatch failures and
worker loss stay visible and resumable. Downloads exclude playlists, preserve
codecs in MKV, and have time/source-size bounds. API and workers need restarting
to activate this code and additive migrations 4/5; old saved edits are protected.

The initial navigation proxy/waveform covers **the first 60 seconds**, with
`coverage_seconds` and full source duration reported separately. Final clips read
the full original. This bounded browser-compatible sample avoids transcoding the
whole recording before the first candidate. A full timeline editor is still
optional Phase 3. The gallery and candidate details refresh during processing;
unrendered candidates expose their hook/reason without broken download links.
Manual review is optional.

| Endpoint | Behavior |
| --- | --- |
| `GET /ingest/library` | Up to 1,000 eligible paths inside configured MEDIA_ROOT |
| `POST /ingest/local/probe` | `{path}` → metadata and audio streams |
| `POST /ingest/local` | `{path, audio_stream_index?}` → imported job ID |
| `PUT /jobs/{id}/audio-track` | `{audio_stream_index: number or null}`; idle override |
| `GET /storage/cache` | Cache size, protection and proposed removals; read only |
| `POST /storage/cache/cleanup` | Explicitly remove inactive derived cache toward configured budget |

Cache identity covers source SHA-256, audio stream, time windows, ASR model/VAD
and adapter version; selection adds transcript digest, prompt/providers/version
and relevant budgets. A selection-setting change reuses ASR. Clip dependencies
include only words overlapping that clip. Automatic recipes refresh changed inputs;
any explicit edit save protects that edit from automatic replacement, as do
pre-migration revisions. Historical renders/revisions remain immutable.

`CACHE_MAX_GB` (20 by default) covers raw `chunks` and `assets` directories. Work
stops with an actionable error if it exceeds the budget; one bounded unit can
cross the limit. Cleanup takes SQLite's write lock and protects active jobs/renders.
It never removes sources, canonical transcripts/highlights, saved edits or final
exports. It does not impose a total-library quota or run automatic eviction.
`MAX_SOURCE_GB` (100) limits each upload/download. Failed download fragments remain
resumable and are not automatically deleted. Review disk usage and remove unwanted
sources/exports explicitly outside this derived-cache cleanup.

See `.env.example` for chunk, VAD/model, text, clip/count/duration, navigation,
cache/source-size and download deadline settings. No dependencies were added.
A synthetic three-hour scheduling test verifies stop/resume and early results;
a representative 2–4-hour recording and measured end-to-end latency/clip quality
remain outstanding. The local library currently has no recording in that duration
range, so the ~20-minute product target is still unverified.
