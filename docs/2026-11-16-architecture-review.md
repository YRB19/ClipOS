---
project: ClipOS
date: 2026-11-16
type: architecture-review-and-decision-log
status: recommendations-pending-selection
tags: [clipos, architecture, video, ai, decisions]
---

# ClipOS review and decision log

Reviewed on 16 November 2026, Asia/Kolkata. Requested by the user: review the existing application, recommend improvements for long gaming streams, editing, video quality, throughput, AI, MCP, and DaVinci Resolve; document decisions in the Obsidian vault.

**Recommendation:** retain React, FastAPI, Redis/RQ, and FFmpeg. Evolve ClipOS into a local-first assistant that discovers moments, prepares editable clip recipes, offers fast review, and renders approved clips from original media. Use Resolve as an optional finishing destination. Add MCP as an interface to stable operations after the underlying pipeline works reliably.

This document separates observed facts from proposed changes. No pipeline refactor, model change, software installation, purchase, MCP connection, or deployment was performed. Existing edits in `captions.py`, `ffprobe.py`, and `llm_fallback.py` were preserved.

## Evidence and immediate problems

| Finding | Evidence | Consequence and recommendation |
| --- | --- | --- |
| Default FFmpeg and ffprobe are currently broken | Direct execution of `/opt/homebrew/bin/ffmpeg -version` and `/opt/homebrew/bin/ffprobe -version` fails: missing `libx265.216.dylib`. `/opt/homebrew/opt/ffmpeg-full/bin/{ffmpeg,ffprobe}` both run successfully. | First repair application binary discovery. Select and validate one FFmpeg/ffprobe pair centrally, using configured paths and stable Homebrew `opt` paths. Check required filters/encoders at startup. A global Homebrew repair is separate work. |
| Binary selection is inconsistent | [captions.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/captions.py:23) and [ffprobe.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/ffprobe.py:7) use version-specific paths; transcription, cutting and reframing invoke plain `ffmpeg`. `FFMPEG_FULL_PATH` is interpreted as a file in captions and a directory in probing. | A captions-only fix cannot repair the whole pipeline. Resolve both binaries once and use them in every module. |
| A captioned deliverable receives three lossy video encodes | [clip_cutter.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/clip_cutter.py:58), [reframe.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/reframe.py:266), [captions.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/captions.py:194). All use libx264. | Avoid full-quality intermediate clips. Analyze proxies; combine trim, crop/composition, scaling, subtitle rendering and optional audio processing in one final video encode from the original. Separate preview encodes are acceptable. |
| All work occupies one queue job | [queue.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/queue.py:45) sets a fixed 30-minute timeout. [pipeline.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/pipeline.py:58) executes every stage and clip in sequence. | Multi-hour streams need checkpoints, duration-aware task sizing, cancellation, stage retries and bounded concurrency. Do not simply raise the timeout indefinitely. |
| Failures do not propagate to RQ | Pipeline exception handlers mark the database failed and return normally. | RQ can consider execution successful although ClipOS failed. Persist the failure and propagate an exception to the queue; reconcile timeouts, killed workers and stale heartbeats. |
| Partial cut failures are mishandled | `cut_clips` returns successful cuts, but pipeline ignores that result and reloads every candidate ID from highlights. | A failed cut can be attempted during reframing and stop other clips. Track success/failure per clip and continue independent candidates. |
| Duplicate starts and queue status are fragile | [jobs.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/routers/jobs.py:110) checks `ingested` but does not atomically claim work or persist the returned RQ ID. Status polling scans queue registries. | Add an atomic run claim, run ID and persisted task IDs. Read task status directly; distinguish queued, executing, retrying, failed and completed. |
| Highlight detection only sees speech | [highlights.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/highlights.py:116) sends the whole transcript in one request and asks for 3–6 clips total. | Long streams can exceed practical context/latency limits and visual events can be missed. Analyze overlapping windows, merge duplicates, and use audio/visual signals as candidate inputs. Make output count or selection budget configurable. |
| Reframing is oriented toward talking heads | [reframe.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/reframe.py:22) decodes frames, detects every fifth frame, picks the largest Haar-cascade face, carries its position forward and smooths it. | Gameplay faces, a small facecam or scene changes can give unsuitable crops. Prefer user-selected facecam/gameplay regions, layout templates, confidence thresholds and manual correction. |
| Captions display one isolated word at a time | [captions.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/captions.py:173) generates one Dialogue event per word, all highlighted. | Add phrase grouping, punctuation-aware breaks, readable line limits, active-word emphasis, editable text, brand styles and platform-safe placement. |
| The frontend offers review/download rather than editing | [ClipDetailPage.tsx](/Users/yoboxo/Developer/ClipOS/clipos-frontend/src/pages/ClipDetailPage.tsx:61) has original/vertical playback and a burn button, but no trim, transcript, crop or caption editor. Burn completion is held only in component state. | Build a focused clip-review editor with durable export states; derive available variants from backend artifacts. Preview the captioned result and persist render status across refreshes. |
| Transcription is CPU-only | [transcribe.py](/Users/yoboxo/Developer/ClipOS/clipos-backend/app/core/transcribe.py:16) hardcodes faster-whisper base/CPU/int8 with word timestamps and no explicit VAD settings. | Evaluate a hardware-appropriate ASR adapter, VAD and overlapping chunks. Preserve the canonical word schema and original media time offsets. |

Local hardware observed: Apple M4, 16 GB unified memory. The working FFmpeg build exposes `h264_videotoolbox`, `hevc_videotoolbox`, `libx264`, `ass`, `subtitles`, and `loudnorm`. Encoder/filter presence was verified; hardware encoding performance and output quality were not benchmarked.

## Proposed architecture

```mermaid
flowchart TD
  A[Import or link original media] --> B[Probe and fingerprint source]
  B --> C[Proxy, audio and waveform artifacts]
  C --> D[Chunked transcription and inexpensive event signals]
  D --> E[Candidate windows]
  E --> F[Selective AI review, ranking and deduplication]
  F --> G[Review: trim, transcript, captions and layout]
  G --> H[Versioned edit recipe]
  H --> I[FFmpeg render from original source]
  H --> J[Optional Resolve timeline handoff]
  I --> K[Validate and expose deliverables]
```

Use one application codebase with independently runnable worker processes. Keep Redis/RQ; create bounded queues for ingest/analysis/render as needed. The database should track assets, pipeline runs, stage attempts, clips, edit revisions and render outputs. Introduce these additively with migrations; retain existing job IDs and transcript/highlight fields. Store large artifacts in files, not Redis. Use atomic artifact writes and validate outputs before marking stages complete.

The edit recipe should reference original source intervals, layout regions, crop keyframes, caption corrections/styles, audio settings and export preset. Give recipes a schema version and revision ID. Cache keys should include the source fingerprint plus the relevant model, prompt, algorithm and settings versions. Caption changes should invalidate caption preview/final render, without retranscribing. A crop change should invalidate composition/render, without rerunning highlight discovery. Retry operations must not overwrite approved edits.

Keep SQLite for the initial single-machine workflow, with short transactions and controlled writers. Move to PostgreSQL when concurrent writers, multiple hosts or shared operation make that necessary. Object storage and remote GPU workers are later options justified by measurements. A local folder/library import avoids uploading already-local multi-gigabyte recordings. Remote imports should enqueue downloads rather than hold an HTTP request open. Consider resumable transfers when remote uploads become part of the workflow.

## Editing and output quality

The first editor should support trim handles, waveform/transcript navigation, caption correction, draggable/resizable source regions, layout presets, undo and preview/export. Prioritize keyboard shortcuts, accept/reject/extend actions, and quickly reviewing many candidates. Show the moment-selection reason and uncertainty rather than implying a model score is a measured probability of virality. A full professional timeline can remain in Resolve.

For gaming streams, provide three initial layouts: gameplay plus a dedicated facecam panel; full gameplay fitted into the canvas with a designed background; and a manually chosen action crop. Protect HUD, minimap, subtitles and important action regions. A 1920×1080 image cropped to 9:16 retains only roughly 608×1080 pixels before upscaling; a 1080×1920 output does not recreate the discarded detail. Choose composition before applying sharpening or upscaling. Reset tracking at scene boundaries; use actual presentation timestamps for variable-frame-rate sources rather than only frame-count/rounded-fps estimates.

Retain the best available original recording and avoid downloading/re-encoding it repeatedly. Support OBS-style MKV/multiple audio tracks through probing and optional remuxing, with original files preserved. Record microphone and game audio separately where possible; retain separate facecam/gameplay sources when available. Select the microphone track for ASR and allow independent mixes for exports. Apply restrained normalization/limiting and optional ducking, preserving game effects and speech. Avoid aggressive denoising of already compressed footage.

Use explicit export presets with dimensions, rational frame rate/timebase, pixel format, codec, bitrate/quality controls and color handling. Preserve genuine 60 fps gameplay where appropriate. Use H.264/AAC MP4 and browser-friendly playback as an initial interoperability target, then validate actual destination needs. Compare libx264 quality settings against VideoToolbox at comparable visual quality; do not apply CRF settings to hardware encoders as if they were equivalent. CPU subtitle/composition filters can remain a bottleneck even with hardware encoding. HDR inputs require deliberate SDR tone mapping for SDR exports; merely changing color tags is insufficient. FFmpeg provides composition/subtitle filters and loudness normalization: [filter documentation](https://ffmpeg.org/ffmpeg-filters.html).

Validate final dimensions, duration, audio presence, A/V synchronization, caption bounds, clipping and representative playback. Persist final source timestamps after clamping so captions and cuts use the same intervals. Use temporary outputs and rename only after successful validation.

## AI strategy for long gaming streams

Use inexpensive signals to propose windows: speech/transcript content, laughter/shouts/audio changes, scene boundaries, optional available chat bursts, and optional manual hotkey markers. These signals are noisy; action volume alone does not establish a good clip. Keep silent gameplay highlights eligible even when no speech is present. An empty transcript should not necessarily fail the whole project.

Start with overlapping analysis windows, for example 5–10 minutes with contextual overlap, and tune on actual streams. Preserve absolute timestamps, merge repeated events across boundaries, and retain setup and payoff. Ask a language model to identify narrative quality using transcript and signal metadata. Send short candidate excerpts or sampled frames to a vision-capable model only where needed. Sparse frames help with composition but can miss fast action; temporal context matters. Score and filter candidates before expensive full-resolution render work.

Require structured outputs and validate finite timestamps, bounds, duration, overlap, valid IDs and score ranges. Treat schema/semantic validation failures as retry/fallback conditions; the current provider chain only falls back when the call/content extraction itself raises, while JSON parsing happens later. Put models in configuration and record provider, model, prompt version, latency and cost. Current highlight-specific provider tiers and shared defaults differ; documentation also names older choices. Choose models with a small representative evaluation set rather than by model size or a nominal free tier. Use request deadlines and retry/backoff policies that distinguish rate limits, transient errors and permanent errors. Remove even partial API key logging from the NVIDIA client.

For this M4 machine, benchmark MLX Whisper with word timestamps against the current CPU baseline. Keep model size selectable; better transcription can cost more compute. MLX supports the required word timestamp interface: [MLX Whisper documentation](https://github.com/ml-explore/mlx-examples/tree/main/whisper). For a future NVIDIA host, faster-whisper offers batching and CUDA execution: [maintainer documentation](https://github.com/SYSTRAN/faster-whisper). Preserve silence offsets when using VAD and deduplicate overlapping chunk words. Test game names, overlapping speech, accents and mixed-language commentary actually present in the user's recordings. Start with one ASR worker and one render worker on 16 GB; tune concurrency against peak memory, interactive responsiveness and thermal behavior.

Current RQ is pinned and locally installed at 2.0.0. Evaluate a tested upgrade to use `SpawnWorker` (introduced in 2.2.0) for macOS rather than relying on disabling Objective-C fork safety. This is still RQ, not a queue-library migration. A long-lived inference process is a separate option for keeping models loaded; merely selecting a spawning worker does not guarantee model reuse across jobs. See [RQ worker documentation](https://python-rq.org/docs/workers/).

## DaVinci Resolve and MCP

Recommend Resolve as an optional destination for stronger pacing, audio mixing, color and bespoke effects after ClipOS narrows a stream to good candidates. Start by exporting selected source ranges, source media references, markers and SRT subtitles with a tested timeline interchange format such as OTIO. Validate exact trim points, frame rates, audio mapping and relinking against the installed Resolve version. Animated ASS styles, arbitrary layout transforms and effects should not be assumed to round-trip; some require explicit recreation through an adapter or a flattened render. Blackmagic documents OTIO interchange in its [new-features guide](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_18.5_New_Features_Guide.pdf).

As of this review, Blackmagic's Resolve 21.1 announcement documents native AI-assistant integration for project analysis, media organization, highlight edits and batch rendering. Its current feature matrix lists AI Assistants requiring an MCP-compatible assistant, and scripting, as Studio features. Prefer evaluating this official integration before choosing a community bridge. Check the installed edition/version and supported operations with a small project before paying for Studio solely for automation. Sources: [official release](https://www.blackmagicdesign.com/media/partial/release/20260908-03), [official feature matrix](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_Studio_21_Features.pdf). Resolve Free is a reasonable manual-finishing trial; advanced automation needs the edition check above.

For ClipOS itself, use normal Python functions/APIs and queue workers for the production media pipeline. MCP standardizes AI access to tools/data; it does not accelerate encoding or supply editing quality by itself. A later thin MCP interface could expose `search_moments`, `create_clip_draft`, `update_clip_edit`, `request_render`, and `get_render_status`. Long operations should return task IDs. Validate edit recipes server-side; give tools project-scoped media access. The same versioned recipe should drive UI edits, AI-requested edits and renders. See [MCP introduction](https://modelcontextprotocol.io/docs/getting-started/intro).

The Obsidian log is plain Markdown in a local vault, so an Obsidian MCP connection is unnecessary for this documentation request. No connector was installed or connected during review.

## Recommended implementation order and measurement

1. **Restore reliable operation:** central FFmpeg pair selection and startup checks; correct task failure reporting; persist queue IDs; prevent duplicate starts; retain successful clips when a peer fails; add real stage/clip progress and retry/resume.
2. **Improve the core experience:** versioned edit recipes, source fingerprints, proxies, early candidate ranking, gameplay layout presets, editable phrase captions and one-pass final rendering. This is the first useful vertical slice.
3. **Handle long streams:** overlapping transcription/analysis chunks, bounded workers, VAD, multimodal candidate review, artifact caching, incremental candidate availability and cancellable exports.
4. **Add finishing/integrations:** tested Resolve handoff; evaluate native Resolve MCP if desired; add a thin ClipOS MCP surface after its API is stable. Add remote GPUs or distributed storage only if measurements justify them.

Before promising a speedup, measure a representative 10-minute recording and a 2–4-hour gaming stream. Include fast action, facecam, quiet gameplay, mixed speech, and the actual source frame rate/resolution. Record stage wall time, real-time factor (processing time/source duration), cold/warm model startup, CPU/memory, model/API costs, time to first useful preview, final render fps, and disk use. Assess accepted clips per reviewed candidate, missed manually marked highlights, caption accuracy and crop failures. Compare old/new outputs visually at comparable quality, not just equal bitrate. Include cancellation, duplicate-run, worker-crash, malformed-model-output and resume scenarios in integration verification. Existing mocked tests do not establish real media quality or throughput.

## Decision register

Append future entries with date, status, decision, rationale, alternatives, affected files and validation. Change a recommendation to accepted only when selected by the user or implemented within authorized scope. Implementation outcomes and reversals should be appended rather than silently rewriting history.

| ID | Status on 2026-11-16 | Decision and rationale |
| --- | --- | --- |
| DOC-001 | Implemented | Use `/Users/yoboxo/Developer/Developer/ChatGPT Data/ClipOS/` for the requested Obsidian documentation, with this report mirrored in the repository for reviewability. |
| DOC-002 | Implemented | Make this turn an assessment and documentation change; preserve existing application edits and distinguish proposed architecture from implemented behavior. |
| ARCH-001 | Proposed | Retain the main stack and evolve to staged, resumable execution rather than a ground-up rewrite. |
| MEDIA-001 | Proposed | Centralize binary configuration first because the current default binaries fail at process startup. |
| MEDIA-002 | Proposed | Preserve originals, analyze proxies and perform one final video encode for each approved recipe. |
| EDIT-001 | Proposed | Build a focused review editor and game-aware composition presets before a full timeline editor. |
| AI-001 | Proposed | Use multimodal candidate selection with bounded analysis windows and strict validation; rank before rendering. |
| PERF-001 | Proposed | Benchmark MLX ASR and VideoToolbox on the existing M4/16 GB machine before recommending new hardware or making speed claims. |
| DATA-001 | Proposed | Add run/clip/edit/render records and migrations without renaming canonical schemas; retain SQLite initially. |
| INTEGRATION-001 | Proposed | Use Resolve for optional finishing and evaluate its official Studio MCP integration before community bridges. |
| MCP-001 | Proposed | Add MCP as a thin optional interface over reliable ClipOS operations; keep normal APIs/queues responsible for execution. |

## Validation performed for this review

Read repository conventions, pipeline, queue, workers, models, ingest, ASR, highlight/scoring/provider logic, cutting, reframing, captions, key frontend pages and relevant tests. Checked the local hardware class and memory, installed RQ/faster-whisper versions, default and alternate FFmpeg/ffprobe execution, and alternate build encoder/filter availability. Checked current primary-source documentation for the proposed external integrations. No full pipeline or speed/quality benchmark was run, no API keys were read from `.env`, and no application source files were changed.

## Follow-up — Implementation planning, 2026-11-16

The user requested a plan for edits. Created [[2026-11-16-implementation-plan]] with seven ordered phases, affected modules, completion checks and a first delivery milestone covering reliability, edit recipes/rendering and the focused editor. PLAN-001 records the documentation work; PLAN-002 through PLAN-005 remain proposed in that plan. No application edits, purchases or connections were started. This follow-up preserves all earlier findings and decision statuses.

## Follow-up — User-confirmed outcome, 2026-11-16

The user clarified the governing goal: a 3-hour gaming stream should become roughly 10 strong, nicely edited, upload-ready clips in about 20 minutes. Automatic selection, editing, rendering and validation are the normal path; manual review/editing is an optional override. This supersedes the review's earlier editor-first delivery emphasis and PLAN-002's first-release proposal, while preserving their history.

GOAL-001 records the accepted user requirement in [[2026-11-16-implementation-plan]]. That plan now prioritizes an automatic end-to-end prototype, consistent output quality and then measured latency against representative full streams. It includes a provisional timing budget and separates network transfer from compute measurements while conservatively accounting for both in the total. Required hardware and actual feasibility remain unverified. No application changes were performed for this clarification.


## Implementation decision — Phase 1, 2026-11-16

**Status: implemented (fixture/integration validated); representative-stream baseline pending.**
The user authorized phase-by-phase implementation and requested a stop after each
phase. Phase 1 is implemented; phase 2 has not started. Existing captions/probing
intent was consolidated into the shared resolver; the user's LLM fallback edits
were preserved. The automatic end-to-end goal remains the governing requirement.

| ID | Status | Decision and rationale |
| --- | --- | --- |
| REL-001 | Implemented | Use one capability-checked FFmpeg/ffprobe pair for every media operation and yt-dlp merging. Prefer the working stable Homebrew ffmpeg-full opt path; support explicit executable paths and both historical FFMPEG_FULL_PATH forms. No Homebrew packages changed. |
| REL-002 | Implemented | Add versioned, transactional migrations and pipeline_runs/stage_attempts/clip_states. A nullable unique active_key atomically permits one active run per job. Persist each task ID before enqueue and fence ambiguous late delivery after enqueue failure. Canonical schemas remain compatible. |
| REL-003 | Implemented | Queue individual analysis/clip work units, record timings/heartbeats and bounded deadlines, reuse validated input/output checkpoints, and reconcile dispatch gaps/lost workers. Retry directly identifiable transient network/429/5xx failures at most three times per unit/run. Preserve independent clips when a peer fails, while raising failed attempts to RQ. |
| REL-004 | Implemented | Add safe-boundary cancellation, idempotent run/resume APIs, detailed progress and durable output-variant discovery. Prevent manual artifact mutations during active automatic execution. Partial batches retain usable clips and map to failed in the legacy job while exposing completed_with_errors in run state. |
| REL-005 | Implemented | Include caption burn-in in automatic processing now, aligning reliability work with the accepted no-required-review target. Continue using the existing multi-pass renderer until phase 2 introduces recipes and one-pass rendering. Promote outputs atomically after validation. |
| REL-006 | Implemented | Isolate tests from the working library/provider APIs/shared Redis. Use a temporary private Redis Unix socket for integration checks and a copied SQLite library for migration verification. No model inference, paid API calls or destructive library operations were used. |
| REL-007 | Accepted, pending measurement | Treat phase 1 as the reliable execution/instrumentation foundation. Do not claim M0's representative baseline, M1's three-hour automatic result or the 20-minute target is achieved from synthetic fixtures. |

**Affected files:** backend config/main/worker, models/job.py and new
models/execution.py, app/migrations/, core/media.py/artifacts.py/execution.py,
core/pipeline.py/queue.py, FFmpeg/ffprobe callers, atomic transcription/highlight/
scoring writes, jobs API and manual-stage guards. Frontend API types, progress/status
rail, job/gallery navigation, clip player/download state, focused tests, backend
README and project SKILL.md were updated. Full details remain reviewable in the
working-tree diff; no commit or production service restart was performed.

**Validation:** 91 backend tests passed in an isolated temporary application copy,
including real FFmpeg extraction/probe/cut/reframe/ASS burn, 1080×1920 output,
audio presence/duration agreement, concurrent starts, duplicate delivery, bounded
transient retries, enqueue ambiguity, cancellation, invalid checkpoints, real RQ
failure and actual workhorse-process termination followed by checkpoint resume.
Seven frontend tests passed; TypeScript/Vite production build and git diff whitespace
checks passed. The build emits nonfatal CSS directive warnings. The real forked
worker test emits Python's multithreaded-fork deprecation warning; the planned
RQ/macOS worker evaluation remains phase 6.

Migrated a backed-up copy of the default local SQLite library twice: all 13 jobs
and their existing field values were preserved. The original library was not
migrated or modified by verification. Temporary backup:
`/var/folders/rp/qnp030cn1nzflxjc7gw9xmnr0000gp/T/clipos-library-migration-mmg0xw1b/library-backup.db`.
Repeatable synthetic legacy-schema migration is also covered by tests.

**Limits / next phase:** no representative gaming-stream quality or throughput
benchmark was run. ASR remains a single bounded task until phase 4. Provider wrappers
that collapse errors into RuntimeError remain manually resumable; provider error
policy belongs to phase 5. Source identity currently uses immutable job-media
size/mtime; full versioned fingerprints/caches belong to phase 4. Existing face
tracking and word-at-a-time caption styling remain until their planned changes.
Next authorized work package to discuss is phase 2: edit recipes, proxies and
one-pass final rendering. Work stops here as requested.

Implementation consulted the installed RQ 2.0.0 API and official
[RQ failure/retry documentation](https://python-rq.org/docs/exceptions/); the queue
library and dependency versions were retained.


## Implementation decision — Phase 2, 2026-11-16

**Status: implemented (short-media, API, queue and regression validated).** The user
requested the next phase after reviewing Phase 1. Phase 2 is complete at its stated
fixture acceptance checks; work stops here. Phase 4 has not started. The revised
automatic delivery order remains 1 → 2 → 4 → 5 → 6; phase 3 is optional correction UI.

| ID | Status | Decision and rationale |
| --- | --- | --- |
| RECIPE-001 | Implemented | Add strict schema-version-1 edit recipes and immutable edit revisions, independent of AI highlight metadata. Bind to the job/source SHA-256 fingerprint, absolute source intervals, normalized displayed-source regions, output-local caption/keyframe times, selected audio indexes/gains and explicit export settings. Atomic expected-revision saves reject stale requests with HTTP 409. Automatic retries preserve saved edits. |
| MEDIA-003 | Implemented | Register source fingerprints and expanded stream/rational-frame-rate/sample-aspect/color/audio metadata. Reuse versioned proxy, thumbnail and waveform artifacts after signature checks; preserve original media. Cache a registered fingerprint while original size/mtime remain unchanged. |
| RENDER-001 | Implemented; realizes MEDIA-002 for final renders | Replace the automatic intermediate cut/reframe/burn dependencies with per-clip recipe and direct-source render units. One final video encode combines interval trims/concatenation, composition/scaling, crop keyframes, ASS and audio mixing. Keep legacy files and handlers readable/runnable. |
| LAYOUT-001 | Implemented | Provide full-gameplay fit on a solid background, gameplay plus a supplied facecam region, and positioned/animated crop. Default to full-gameplay fit so an unverified facecam/action crop does not discard the HUD. Automatic facecam discovery and representative gaming composition quality are not established in this phase. |
| RENDER-002 | Implemented | Pin queued preview/final work to immutable revision IDs, deduplicate active/valid completed work, retain durable IDs/errors/heartbeats, reconcile lost tasks and fence ambiguous enqueue delivery. Render to unique revision-associated paths and promote verified files atomically; keep old renders and legacy variants. |
| EXPORT-001 | Implemented, limited preset | Use 1080×1920 final / 360×640 preview, H.264/AAC MP4, yuv420p, square pixels and rational 1–60 fps. Mix explicitly selected audio tracks with gain/limiting, preserve delayed-audio timing, and provide silent AAC when no tracks are selected. Reject HDR/rotated final inputs pending the deliberate conversion work planned for phase 6. |
| API-001 | Implemented | Expose edit/save, assets and queued preview/final/history APIs with durable availability and current-revision flags. Gallery/player/download views use current final renders; legacy variants remain accessible. Serve raw/clips configured roots independently, preserving canonical media URLs. Full editing controls remain optional phase 3. |

**Affected files:** new backend `models/edit.py`, `core/recipes.py`, `core/assets.py`,
`core/renderer.py`, `core/renders.py`, `routers/edits.py` and `tests/test_edits.py`;
migration 3; expanded ffprobe metadata; pipeline/queue/reconciliation/main/jobs;
isolated test fixtures and Phase 1 queued-unit regression tests. Frontend API
schemas, clip/gallery/player/status display and recovery tests; SKILL.md and backend
README. No new dependencies, global package changes, commit, or deliberate live
service restart was performed.

**Validation:** **111 backend tests passed** (26.71 seconds for the final fixture
suite), **8 frontend tests passed**, TypeScript/Vite production build and diff
whitespace checks passed. New real-media checks cover all three final layouts,
preview rendering, rational 30000/1001 frame rate, multiple intervals/tracks, silent
source audio, delayed-source audio, source-file preservation, subtitle text/control
escaping, and visible trim/caption timing. Frame samples verify reordered blue/red
source ranges and caption appearance only during the saved local interval. Visually
inspected a three-layout fixture contact sheet. API checks include save/reload,
concurrent revision conflicts, stale render revision flags, immutable output
history, enqueue/lost-worker recovery, byte-range MP4 downloads and navigation
thumbnail URLs. A private Redis/RQ worker completed a real preview render. Automatic
pipeline and caption-only/crop-only resume tests verify that ASR/highlight/scoring
calls are not repeated and no high-quality intermediate clip is required.

Migration 3 ran twice against a backed-up copy of the default local library,
preserving all **13** original jobs and their existing field values. The working
library was not migrated by verification. Temporary backup:
`/var/folders/rp/qnp030cn1nzflxjc7gw9xmnr0000gp/T/clipos-phase2-migration-fo5wec7x/library-backup.db`.
Existing nonfatal CSS directive build warnings and the fork-worker test warning
remain; the RQ/macOS worker evaluation stays in phase 6.

**Limits / milestone accounting:** no full gaming-stream inference or quality/
latency benchmark was run, so M1 and the approximately 20-minute target remain
unverified. Proxy generation and ASR are still bounded whole-source units until
phase 4 adds resumable chunks; initial asset preparation adds real work that must
be included in end-to-end timing. Preview and final share recipe semantics but
are separately encoded from the original; the source proxy is a reusable navigation
asset, not a required final intermediate. Defaults do not infer a facecam region or
perform phrase-caption editing. HDR/rotation conversion, richer selection/pacing
and the optional full editor remain later packages. Next is phase 4: multi-hour
imports, audio selection, overlapping chunk analysis and resumable caches.

Implementation checked the official [FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html)
and [FFmpeg command/stream documentation](https://ffmpeg.org/ffmpeg.html), then
validated the installed binary's actual output with fixtures rather than inferring
media correctness from mocked command calls.


## Phase 4 implementation decisions — 2026-11-16

**Status: implemented; fixture/integration validated; representative-stream
acceptance pending.** The user requested the next phase after Phase 2. This
package follows the accepted 1 → 2 → 4 → 5 → 6 order and stops before Phase 5.
The target remains a three-hour gaming stream → roughly ten strong upload-ready
clips in about twenty minutes, not a measured result.

| ID | Status | Decision and rationale |
| --- | --- | --- |
| INGEST-002 | Implemented | Accept original MP4/MKV/MOV/WebM/M4V uploads and local references within configured MEDIA_ROOT. Resolve symlinks inside the root, probe available tracks, and preserve originals/stream indexes. Remote YouTube requests return a durable job ID with HTTP 202, then run download as a normal fenced RQ stage with timeout, playlist and per-source-size bounds. Failures/loss remain visible and resumable. |
| ASR-002 | Implemented | Prefer named microphone/voice/commentary streams, including MP4 handler labels; allow an explicit stream override. Apply VAD through faster-whisper, pad delayed audio and add extraction offsets to restored word timestamps. Preserve separate tracks for export; explicitly named game/desktop plus mic defaults mix game at 0.5 gain and voice at 1.0. Unnamed/composite sources retain their first-track mix. |
| LONG-001 | Implemented | Replace whole-source automatic ASR/selection with overlapping, separately bounded RQ chunks (default 300s core, 15s context each side). Persist versioned, digest-verified JSON checkpoints, including empty results. Merge absolute words with overlap ownership/deduplication and keep boundary padding available to early captions. Split dense text into overlapping bounded prompts. Successful chunks survive cancellation/worker loss and missing aggregates can be reconstructed without repeating inference. Fingerprinting stays in the worker before chunk expansion. |
| SELECT-002 | Implemented foundation; visual quality pending Phase 5 | Publish candidates after each analysis chunk with stable source/cache/range-derived IDs, overlap deduplication and configurable count/individual/total duration budgets (10, 15–90s, 600s defaults). Silent chunks retain explicit zero-score, unassessed visual eligibility; this does not claim a detected highlight. Early candidates can render before later sections are analyzed; later selection may replace them while prior output history remains. |
| CACHE-001 | Implemented | Key ASR by source fingerprint, selected stream, time window, adapter/model/VAD settings; key selection additionally by transcript digest, prompt/provider/version and budgets. Selection changes reuse ASR. CACHE_MAX_GB guards derived chunk/navigation storage, with explicit planned cleanup of inactive cache only under SQLite's write lock. Sources, canonical aggregates, edit revisions and exports are outside deletion scope. Active runs/renders are protected. Source-size limits are per upload/download; no total-library quota or automatic eviction is claimed. |
| RECIPE-002 | Implemented; extends RECIPE-001 | Invalidate a clip only for overlapping transcript words and its own selection/source inputs. Mark generated recipe heads so changed boundary captions can refresh automatically. Explicit saves clear the marker; human corrections and pre-migration revisions remain protected, and historical revisions stay immutable. Migrations 4/5 add import options and the nullable generation marker without rewriting job fields. |
| MEDIA-004 | Implemented; supersedes full-source initial navigation work | Prepare only the initial configurable navigation sample (60s default), including browser-compatible proxy and waveform; expose coverage separately from full source duration. Full-source hashing/probing and final direct-source rendering remain. This removes a whole-stream proxy encode from the time-to-first-candidate path. A complete timeline proxy/editor remains optional later work. |
| UX-002 | Implemented | Add local-library/track controls and chunk progress. Gallery and early details refresh as sections arrive, showing hooks/reasons/status before rendering. Hide unavailable video/download/burn actions. Remote ingestion navigates directly to durable progress; automatic processing requires no review step. |

**Affected files:** new backend `core/chunks.py`, `core/imports.py`, `core/cache.py`,
`models/source_import.py`, `routers/storage.py` and `tests/test_chunks.py`; settings/
.env example, migrations 4/5, ffprobe track labels, original-path legacy cutting/
transcription, assets, recipe heads/default mixes, scoring, pipeline/queue/
reconciliation, ingest/jobs/edit routes and main registration. Frontend local
import form, API types, progress, gallery/card/details and long-recording tests;
SKILL.md, backend README and these two documentation mirrors. No dependencies
were added. No deliberate live service restart or working-library migration was
performed during verification.

**Validation:** backend and frontend suites, production build/lint and whitespace
checks are recorded in the matching phase checkpoint. Real FFmpeg fixtures cover
MKV multiple audio tracks, MP4 handler labels, selected microphone extraction,
boundary-word deduplication, delayed-source silence, immutable source bytes and
actual direct-source final rendering. Mocked providers cover bounded dense prompts,
invalid timestamps, stable candidate identity, overlap/count/duration selection,
cache invalidation, silent ASR and failed/successful queued downloads. A synthetic
three-hour timeline exercises 36 persisted five-minute ASR/selection units,
early recipe/render scheduling, cancellation and resume without repeating successful
sections. This uses mocked inference/encoders/queue transport and is not a real-time
factor or quality benchmark. Existing private Redis/RQ failure/render regressions
also run. Explicit cleanup tests protect active jobs, original media and saved edit
history. Recipe tests refresh affected automatic captions and preserve human edits.

Migrations 4/5 were applied twice to a backup copy of the default library, preserving
all **13** jobs/field values. A version-4 fixture without the generation marker also
upgrades repeatedly while preserving existing edits as protected. Backup:
`/var/folders/rp/qnp030cn1nzflxjc7gw9xmnr0000gp/T/clipos-phase4-migration-skcymstj/library-backup.db`.

**Remaining validation / limitations:** the current library has 11 available
sources, the longest about 1,142 seconds, and no 2–4-hour source. A representative
stream therefore has not been processed or reviewed. Real VAD/ASR accuracy,
selection/pacing, total memory/disk/API costs and end-to-end latency remain to be
measured. Initial source hashing is full-source work. Early renders may be replaced
by later candidates; rank-before-high-quality-render work remains Phase 5. Derived
cache limits can be crossed by one bounded unit before stopping; inactive cleanup
is explicit, original/export storage is user managed, and download fragments are
retained for resume. Full timeline navigation is not supplied by the initial sample.
Silent candidates have no visual assessment yet. Existing RQ/macOS fork and
nonfatal frontend CSS/lint warnings remain Phase 6/existing cleanup work. No twenty-
minute claim is made. Stop here; next authorized package is Phase 5.


**Final verification addendum — 2026-11-16, implemented:** cache cleanup also
retains database-referenced originals and exports if a user imports/stores them
inside a cache directory. This closes a nested-path deletion risk without adding
a permission step or automatic eviction. Affected: `core/cache.py` and its source-
retention fixture. All 10 chunk/import/cache tests passed after this last guard;
the full 123-test backend suite and 12-test frontend suite, build/lint and
whitespace checks passed for the phase. Obsidian/repository copies were verified
identical after appending. Phase 5 remains untouched.


## Phase 5 implementation decisions — 2026-11-16

**Status: implemented; fixture/integration validated; real-stream comparison
pending.** The user requested the next phase after Phase 4 and this package stops
before Phase 6. The three-hour → roughly ten upload-ready clips in about twenty
minutes remains an unverified product target.

| ID | Status | Decision and rationale |
| --- | --- | --- |
| SELECT-003 | Implemented; supersedes SELECT-002's early render order | Gather transcript and optional audio/scene/marker candidates in bounded chunks, retain the full `candidates.json` pool, then make a balanced global shortlist and rank before creating recipes or high-quality exports. This prevents expensive speculative final renders. Preserve setup/payoff context and canonical `highlights.json` keys for selected clips. |
| SELECT-004 | Implemented with quality evaluation pending | Audio reaction peaks and scene changes create candidate windows, not quality claims. Sparse-frame visual assessment is available only with explicit image-capable OpenRouter model configuration. Unassessed silence cannot win solely on an LLM score; confident observed visual payoff or a manual marker can ground it. Conservative replay suppression requires a matching specific event and similar nonuniform sampled frames. Audio, scene and vision switches default off until paired real-recording measurements justify their latency/cost. Chat signals are deferred because no synchronized chat source is available. |
| SELECT-005 | Implemented | Validate provider JSON and semantics before accepting a response: IDs, finite scores, interval/source bounds, durations and complete ranking coverage. Invalid responses fall through provider tiers. Ordered model configuration, request/chain deadlines, bounded transient retries/backoff and safe error summaries avoid leaking API key fragments or raw provider bodies. Record per-call latency, stage, model, prompt version, token counts and OpenRouter cost only where supplied. Unknown cost stays unknown. |
| SELECT-006 | Implemented | Migration 6 adds fingerprint-bound moment markers, optional accepted/rejected/automatic feedback and durable model-call telemetry. Marker/feedback writes reject active runs; resume invalidates selection/ranking but reuses valid ASR. Gallery and detail views can show reasons, candidates and review controls without requiring human approval. A page being open is not counted as review time; only explicit measured time is reported. |
| EVAL-001 | Implemented evaluator; real study proposed | Paired offline evaluation uses the same manually marked truth intervals for transcript-only and combined selection, with one-to-one matching for misses/duplicates and explicit acceptance/review-time fields. The included synthetic hand-authored manifest/report test the evaluator format only. Real 2–4-hour gaming recordings, reference moments, acceptance, corrections and wall-clock/resource measurements are still required for a quality or twenty-minute claim. |

**Affected files:** backend `config.py`, migration 6, `models/selection.py`,
`core/signals.py`, `core/selection.py`, `core/selection_validation.py`,
`core/evaluation.py`, chunks/highlights/scoring/provider clients/fallback,
pipeline/queue/reconciliation, marker/feedback router and job responses; frontend
API types, selection review, gallery/card/details/progress/status views; backend and
frontend tests, `.env.example`, README files, `SKILL.md` and documentation mirrors.
No top-level dependency was added. The normal final render now waits for rank;
Phase 4's early-render behavior is superseded, while manual endpoints and render
history remain available.

**Validation:** final full backend suite **147 passed**; the focused Phase 5
selection suite **24 passed**, frontend **15
passed**, production build and lint succeeded, and `git diff --check` passed.
Real FFmpeg fixtures exercise audio peaks, scene changes, preserved intervals and
sampled image extraction. Mocked provider integration covers silent visual action,
misleading loud noise, replay conservatism, malformed outputs, deadlines, tier
fallback, secret-safe logs, shortlist/rank budgets and batch resume. The offline
evaluator CLI generated the included report from explicitly synthetic inputs;
its reported recall is not model performance. Migration 6 applied twice to a
separate backup copy of the pre-existing library, preserving all **13** job rows
and values; copy: `/var/folders/rp/qnp030cn1nzflxjc7gw9xmnr0000gp/T/clipos-phase5-migration-if94fupm/library-copy.db`.
The working library was not migrated and live API/worker processes were not
restarted as part of this check.

**Remaining validation / limitations:** no representative three-hour stream or
manually marked real comparison was available. The current defaults use transcript
selection with global rank; optional audio/scene/vision must be enabled and
evaluated deliberately. Sparse frames can miss fast payoffs. Live provider costs,
end-to-end latency, upload-ready acceptance and the twenty-minute target remain
unmeasured. Phase 6 will address measured speed/output tuning; no Phase 6 work is
included here.


## Phase 6 implementation decisions — 2026-11-17

**Status: implemented local tuning and verified fixtures; representative-stream
acceptance pending.** The user requested the next phase after Phase 5. This entry
records measured local choices and stops before optional Phase 7. The automatic
three-hour gaming stream → roughly ten upload-ready clips in twenty minutes is
still an unverified target, not an achieved benchmark.

| ID | Status | Decision and rationale |
| --- | --- | --- |
| PERF-001 | Implemented measurement; CPU default retained | Add isolated ASR/render benchmark scripts and publish reproducible numbers. On the same 30-second real speech excerpt, faster-whisper base/int8 took 3.473 s with 92 valid word times; cached MLX base took 3.030 s with one zero-duration word. The first MLX run including model download took 25.268 s. On a 14.49-second known-text generated phrase, CPU was faster (2.227 vs 2.666 s) and had lower WER (2.13% vs 4.26%). These small mixed results and absent real gaming truth do not justify switching or adding MLX to runtime dependencies. The official MLX word-timestamp API was checked, but only the current CPU adapter remains production-selected. |
| ENCODE-001 | Implemented measurement; libx264 default retained | Compare the actual ClipOS 1080×1920 caption/layout graph on two short F1 excerpts against a lossless FFV1 composition. At 50 fps, libx264 CRF 20 took 1.944 s/3.50 MB/SSIM 0.995934; VideoToolbox 8 Mbps took 1.756 s/5.91 MB/0.996431 and 5 Mbps took 1.824 s/3.83 MB/0.994328. At 29.97 fps, libx264 was 1.238 s/2.68 MB/0.968851 versus VideoToolbox 1.096 s/3.87 MB/0.968316. The measured speed gain does not justify the size/quality trade on these samples. No hardware-encoder default or unmeasured speedup is claimed. |
| EXPORT-002 | Implemented; extends EXPORT-001 | Version the renderer output, convert correctly tagged BT.2020 PQ/HLG to BT.709 SDR with `zscale`/tone mapping before layout, explicitly tag known limited-range BT.709 outputs, and reject ambiguous HDR metadata. Keep H.264/AAC/yuv420p and rational CFR validation. Cap automatic recipes from >60 fps sources at 60 fps rather than falling back to 30. Real FFmpeg fixtures render PQ final and HLG preview, VFR input to 60 fps CFR final, and high-source-rate recipe selection. Real HDR appearance remains unreviewed. |
| QUEUE-002 | Implemented with a documented Unix-socket fallback | Upgrade pinned RQ 2.0.0 → 2.6.1 and redis-py 6.0.0 → 7.1.0 after private Redis integration. The macOS TCP worker now uses SpawnWorker without parent CV/ASR preloads; delayed-task scheduler remains. Redis-py 8.1.0 failed the spawn trial because connection arguments were not reconstructable. With redis-py 7.1.0, a private TCP spawn job and finished registry passed. Unix-socket spawn failed with an unexpected `path` argument, so explicit Unix URLs retain the fork worker. Scheduler startup can still fork and warn. Logging reports Redis transport without exposing a credential-bearing URL. |
| PERF-002 | Implemented one-pair probe; increased concurrency deferred | A real 494-second source required 0.462 s fingerprinting and 6.918 s to create the initial 60-second navigation sample in isolated storage. One isolated 30-second CPU ASR plus six-second render trial took 4.979 s serial versus 4.104 s concurrent, but each task slowed under contention. Keep the single default queue/worker until representative multi-job memory/critical-path evidence supports splitting ASR and renders. Preview-resolution and vision-sampling quality tuning await gaming references; vision remains opt-in. |
| ACCEPT-001 | Proposed validation, not implemented result | Run multiple representative cold-cache three-hour gaming streams with manually marked moments and output review. Record first usable preview, all stage/API times and cost, full-process peak memory/disk, correction/acceptance rates, HUD/caption/audio quality and total wall time. The available 19-minute F1 broadcast is only a short technical probe, not the product input or acceptance dataset. |

**Affected files:** `clipos-backend/app/core/renderer.py`, `recipes.py`,
`worker.py`, `requirements.txt`, focused real-media/RQ tests,
`tools/benchmark_asr.py`, `tools/benchmark_phase6.py`, backend/root README,
`SKILL.md`, benchmark report and these decision/checkpoint mirrors. Earlier
uncommitted Phase 5 files were preserved; this phase did not rewrite canonical
transcript/highlight/job schemas or the user's working library.

**Validation:** final backend suite **153 passed**; dependency resolver check and
`git diff --check` passed. Private TCP Redis verified a spawned worker with its
scheduler and finished-job registry; existing private RQ failure/render tests
passed. Real FFmpeg tests exercise tagged synthetic PQ/HLG conversion, VFR→60 fps
final output, >60 fps recipe cap, A/V geometry/duration/audio checks and source
preservation. Short benchmark numbers, methods, machine, memory/size and explicit
unknowns are published in `docs/benchmarks/2026-11-17-phase6.{md,json}`. One
final frame from the F1 sample showed HUD and caption within the output, alongside
large unused vertical areas. This is a narrow visual check, not acceptance.
Frontend code was not changed; no paid provider call, live working-library
migration or service restart was performed.

**Limitations / next validation:** the default Homebrew FFmpeg executable is
still broken on this machine; centralized discovery uses the working ffmpeg-full
pair. First usable preview, full three-hour critical path, API cost, representative
gaming clip quality, real HDR appearance and statistical run variability are
unknown. MLX and VideoToolbox results are one-run local probes; neither is a
product-quality result. The twenty-minute claim remains open. Stop here at the
user's phase boundary.
