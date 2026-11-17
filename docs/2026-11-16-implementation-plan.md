---
project: ClipOS
date: 2026-11-16
type: implementation-plan
status: proposed
tags: [clipos, roadmap, editing, performance, decisions]
---

# ClipOS implementation plan

Prepared on 16 November 2026, Asia/Kolkata, following the user's request to make a plan for edits. This is an implementation roadmap; application changes have not started.

Related assessment: [[2026-11-16-architecture-review]].

## Target workflow and scope

**User-confirmed product goal:** a 3-hour gaming stream becomes roughly 10 strong, nicely edited, upload-ready clips in about 20 minutes. The normal path automatically selects, edits, renders and validates the clips. Human review and manual corrections are optional overrides. This supersedes the earlier manual-review-first interpretation.

The existing M4/16 GB Mac is the starting benchmark machine, not a confirmed sufficient deployment target. Meeting the goal on this machine has not been demonstrated; measured bottlenecks will determine whether optional remote compute is necessary.

Retain React, FastAPI, SQLModel/SQLite, Redis/RQ and FFmpeg. Preserve canonical job IDs, transcript words, highlight fields and existing media. Add migrations and compatibility adapters rather than restructuring the repository. New module/table names below are proposals and must be checked against `SKILL.md` during implementation. Preserve the pre-existing edits in captions, probing and LLM fallback.

The automatic end-to-end path is the first delivery milestone. The phase numbers below remain stable as work-package references, but the updated delivery order is **1 → 2 → 4 → 5 → 6**. Baseline timing starts during phase 1. Phase 3 provides optional corrections after the automatic path works; phase 7 remains optional integration work. See the goal clarification at the end for the current acceptance criteria and milestones.

## Phase 1 — Restore reliable processing

**Edits**

- Centralize FFmpeg/ffprobe resolution in a shared core utility. Configure executable paths explicitly, support stable installation paths, and validate versions plus ASS/encoder capabilities. Adopt one documented meaning for the existing binary-path setting and support its old form where practical.
- Make extraction, probing, cutting, reframing and caption rendering use that utility. Use the working local installation without changing global Homebrew packages.
- Add migration-managed run, stage-attempt and per-clip state; persist RQ task IDs. Keep existing job status fields compatible and expose detailed execution state additively.
- Atomically claim runs to prevent duplicate processing. Handle enqueue failure/reconciliation so a claimed run cannot remain silently stranded.
- Propagate task failures to RQ after recording them. Track successful clips separately, and allow unaffected clips to complete.
- Split orchestration into checkpointed tasks with bounded retries for transient failures, heartbeats and resume from valid outputs. Add cancellation at safe work-unit boundaries and bounded task deadlines.
- Update progress UI to show actual stage, completed/total work, failed clips and retry/resume actions. Persist export state so refresh does not lose it.

**Primary files:** backend `config.py`, `main.py`, `models/job.py`, `core/queue.py`, `core/pipeline.py`, `routers/jobs.py`, all FFmpeg callers, and frontend `ProgressPage.tsx`, `StatusRail.tsx`, `lib/api.ts`. Add focused modules inside existing directories and a migrations location without moving current code.

**Completion checks**

- Every media operation uses the same working binary pair; missing capabilities produce actionable errors.
- Two start requests create one active run. A failed enqueue and a killed worker are recoverable.
- An injected per-clip failure leaves other clips usable; queue and database failure states agree.
- Retry resumes completed work rather than restarting transcription. Existing jobs and APIs remain readable.

**Suggested review units:** binary discovery and diagnostics; execution records/migrations; resumable orchestration and progress.

## Phase 2 — Add edit recipes, proxies and one-pass final rendering

**Edits**

- Define a versioned clip edit recipe: source reference, source intervals, layout, crop keyframes, caption edits/style, audio selections and export preset. Store edit revisions separately from AI candidate metadata.
- Probe source streams, rational frame rates, color metadata and audio tracks. Fingerprint media and create reusable proxy, waveform and thumbnail artifacts.
- Add recipe read/update APIs with revision checks so stale browser requests cannot overwrite newer edits. Enqueue preview and final renders and expose durable output availability.
- Build a renderer that reads the original source and combines trimming, composition, scaling, ASS captions and audio mixing into one final video encode. Keep preview rendering separate.
- Implement three compositions: gameplay plus facecam, full-gameplay fit with a background, and a manually positioned crop. Treat coordinates consistently across source, proxy and output resolutions.
- Write temporary outputs, verify them, and promote them atomically. Existing clip variants stay accessible while new recipes are introduced.

**Primary files:** backend `models/`, `core/clip_cutter.py`, `core/reframe.py`, `core/captions.py`, `core/ffprobe.py`, `routers/clips.py`, `routers/captions.py`, plus proposed edit/render modules and frontend API types.

**Completion checks**

- A recipe can be saved, loaded and rendered reproducibly without modifying its original source.
- A caption-only or crop-only change does not rerun ASR or highlight detection.
- Final rendering starts from original media; high-quality intermediate encodes are not required.
- Verify trim boundaries, 1080×1920 geometry, aspect ratio, audio presence, A/V sync and subtitle timing using short real fixtures.

## Phase 3 — Optional correction and editing experience

**Edits**

- Evolve the clip detail page into an editor: video preview, trim range, waveform, transcript seek, editable captions, layout controls and export actions.
- Group captions into readable phrases with active-word highlighting, punctuation-aware line breaks, selectable styles and safe margins. Escape subtitle text correctly.
- Support moving/resizing facecam and gameplay regions, crop correction, reset and undo. Preview the same recipe semantics used by final rendering; show when a preview is stale.
- Add autosave with visible saved/unsaved/error state and revision conflict handling. Keyboard actions cover playback, setting boundaries, accept/reject and moving to the next candidate.
- Improve the gallery with accepted/rejected/draft/exported states and batch export of selected clips. Label model scores as rankings rather than probabilities.
- Show final captioned output in the player and recover render/download state after refresh.

**Primary files:** `ClipDetailPage.tsx`, `GalleryPage.tsx`, `ClipCard.tsx`, `lib/api.ts`, new editor components, and backend caption/edit APIs.

**Completion check:** a user can select a candidate, fix text, trim it, change layout, undo a change, reload and export the saved edit without leaving ClipOS. Test preview/final agreement and the save/reload/export flow.

**Revised priority:** this editor is an optional correction surface. It does not gate the automatic clipping release, and the normal workflow must not wait for a person to accept candidates or adjust each clip.

## Phase 4 — Make multi-hour sources resumable

**Edits**

- Add local-library import through a user-selected/configured media root; queue remote downloads instead of holding requests open. Preserve originals and support probed MKV/multiple audio tracks, with remuxing when required for preview playback.
- Select the microphone track for transcription where available. Preserve distinct source tracks for final mixes.
- Process transcription and candidate analysis in overlapping chunks with absolute source timestamps, checkpointed outputs and overlap deduplication. Use configurable time/token budgets rather than one whole-stream prompt.
- Introduce VAD while preserving silence offsets. Keep silent visual candidates eligible when ASR returns no speech.
- Define cache keys from source identity and relevant model/prompt/settings versions. Invalidate only affected downstream artifacts. Bound local storage and make cleanup explicit so active sources and approved edits are retained.
- Surface candidates incrementally and allow review while later sections are analyzed. Add configurable clip count/duration budgets and deduplication across chunk boundaries.

**Primary files:** `routers/ingest.py`, `core/transcribe.py`, `core/highlights.py`, `core/scoring.py`, pipeline tasks, asset/run models, `UploadPage.tsx`, gallery and progress pages.

**Completion checks:** process a representative 2–4-hour stream, stop and resume it without repeating successful chunks, keep boundary-crossing speech aligned, and review early candidates before all analysis completes. Test a silent section, mixed audio tracks and a failed download.

## Phase 5 — Improve moment selection with AI

**Edits**

- Generate candidate windows from transcripts, audio reactions, scene changes and manual markers. Add chat signals only when a suitable synchronized source is available.
- Use selective vision/video analysis on shortlisted excerpts to assess action, reactions and visual context. Preserve setup and payoff; combine evidence and merge repeated moments.
- Rank before high-quality rendering. Expose a short selection reason and manual accept/reject feedback.
- Centralize model/provider configuration and record latency, prompt/model version and usage/cost where available.
- Validate timestamps, finite values, duration limits, IDs and scores. Include malformed or semantically invalid model outputs in controlled retry/fallback handling.
- Remove API key fragments from logging and apply request deadlines and transient-error backoff.

**Primary files:** `core/highlights.py`, `core/scoring.py`, `core/llm_fallback.py`, provider clients, settings, proposed event-analysis modules and candidate UI.

**Completion checks:** compare transcript-only and combined selection on the same manually marked recordings. Measure missed highlights, accepted candidates and review time. Include silent action, misleading loud audio, repeated events and invalid model output. Keep each new signal configurable until it demonstrates value.

## Phase 6 — Tune speed and output quality using measurements

Begin baseline measurement after phase 1; repeat comparisons only when relevant pipeline changes land.

**Experiments and edits**

- Benchmark the current CPU ASR against an optional MLX adapter using the same recordings and required word timestamps. Choose model/settings using both accuracy and wall time; retain the existing adapter as a fallback.
- Compare libx264 and VideoToolbox at comparable visual quality. Tune preview resolution, analysis sampling, encoder settings and worker counts within the Mac's memory budget.
- Start with one heavy ASR task and one render task; measure contention before increasing concurrency. Consider a warm inference process only if model load time materially affects throughput.
- Evaluate a tested RQ upgrade/macOS spawning worker as described in the review. Keep dependency changes explicit and record them in requirements.
- Define explicit export settings for frame rate, pixel format, audio, bitrate/quality and color. Test 60 fps gameplay, variable-frame-rate footage and HDR-to-SDR conversion where applicable.

**Completion checks:** record time to first usable preview, per-stage wall time, real-time factor, final render fps, peak memory, disk use and model/API cost. Verify output readability, captions, HUD visibility and A/V sync. Publish measured comparisons before selecting defaults; no fixed speedup is promised in this plan.

## Phase 7 — Optional Resolve handoff and MCP

**Edits**

- Implement export of selected source ranges, source references, markers and SRT with a tested timeline interchange format. Verify relinking and frame-accurate ranges in the installed Resolve edition/version.
- Document which layouts, caption animations and effects transfer, which must be rebuilt, and which require a flattened render.
- Evaluate the official Resolve integration identified in the review on a small test project before depending on it or purchasing software for automation.
- Add a thin ClipOS MCP interface over the existing search, draft/edit, render and status APIs. Return task IDs for long work; reuse recipe validation and project-scoped access.

**Completion check:** edit and render an exported shortlist in Resolve, and run one representative natural-language ClipOS edit/export through MCP without bypassing task tracking or recipe validation. This phase does not block the first release.

## Delivery and verification rules

- Deliver through small reviewable changes in the revised order: 1 → 2 → 4 → 5 → 6, with phase 3 optional correction tools and phase 7 optional integrations. Preserve current application behavior behind compatibility paths until replacements are verified.
- Introduce model/table/API fields additively and use repeatable migrations. Back up the local database before migration verification; test against a copy, not the working library.
- Use isolated temporary storage and fixture media for automated checks. Do not flush Redis, delete current media or use paid model calls in routine tests.
- Run relevant backend tests for changed behavior; run frontend typecheck/build and relevant UI tests for editor changes. Add real FFmpeg integration fixtures because mocked subprocess tests cannot establish media correctness.
- At the first automatic milestone, exercise import → selection → automatic edits → render → quality checks → downloadable deliverables without manual intervention. Test optional correction/save/reload separately. Verify crash/retry/cancel/resume and cache invalidation before claiming long-stream readiness.
- Save each implementation decision, affected files, validation and any plan deviation to the Obsidian decision log and repository mirror. Keep completed findings and historical entries intact.

## Planning decisions recorded on 2026-11-16

| ID | Status | Decision | Rationale and validation |
| --- | --- | --- | --- |
| PLAN-001 | Implemented (documentation) | Produce this phased roadmap without starting application edits. | The user requested a plan. Repository and vault copies are verified on save. |
| PLAN-002 | Proposed | Deliver reliability, edit recipes/rendering and a focused editor as the first release. | Creates a usable end-to-end improvement before adding more model/integration complexity. Completion gates are phases 1–3. |
| PLAN-003 | Proposed | Retain the current stack and canonical schemas; add state and revisions through migrations. | Reduces compatibility risk and protects existing projects. Validate old jobs and migration copies. |
| PLAN-004 | Proposed | Select performance defaults using local benchmarks and quality comparisons. | Existing hardware may be sufficient; measurements determine ASR, encoder and concurrency choices. |
| PLAN-005 | Proposed | Keep Resolve/MCP optional and downstream of a stable API and recipe model. | The core editing workflow should deliver value independently of external integration. |

No implementation status is implied by the proposed entries above. Phase completion and actual benchmark results will be appended as work occurs.

## Goal clarification — 2026-11-16

The user clarified: "a stream fo 3 hrs to 10 or so real good clips of the game in about 20 mins, nicely edditted and ready to upload out the gate". This sets the product outcome and supersedes the earlier editor-first milestone and manual-review default. Earlier decision rows are retained as historical proposals.

### Acceptance contract

- **Input:** a complete approximately 3-hour gaming stream; baseline tests use representative 1080p/60 footage with facecam and game audio, followed by other supported formats. This input profile is a proposed benchmark profile, not an additional user restriction.
- **Output:** approximately 10 distinct, strong moments, automatically edited and exported as upload-ready files. A working evaluation range is 8–12 clips when the material supports it; do not fill the batch with weak or duplicate moments merely to hit a count.
- **Editing:** clean setup-to-payoff boundaries, purposeful pacing, gameplay/facecam composition, protected HUD/action regions, readable synchronized captions where speech is present, balanced audio and restrained template-driven emphasis. Avoid arbitrary zooms, forced transitions or removing pauses that are necessary for the payoff. Editing decisions must work automatically through the recipe/render pipeline.
- **Delivery:** validated playable files with the chosen platform preset, completed captions where appropriate and draft titles/captions for uploading. Ready to upload does not authorize posting to an account.
- **Human effort:** no required candidate selection, caption burn button, per-clip timeline work or external editor in the normal path. Failures or low-confidence cases are clearly surfaced; manual corrections remain available.
- **Latency:** target about 20 minutes for the complete cold-cache run, with all final outputs validated and available. Measure end-to-end wall time rather than summing overlapping task durations. Track import/download, analysis, model calls, rendering and verification separately. The current conservative planning assumption includes transfer time in the total; network/source availability must be reported so it cannot be confused with compute performance. Exact network conditions and the timing start point remain to be agreed for a formal service guarantee.
- **Quality evaluation:** use a fixed set of real gaming streams and reference edits/marked moments to judge whether outputs can be uploaded without mandatory changes. Count needed corrections, duplicate/weak selections, cut-off payoffs, bad crops, caption errors and audio problems. A fast run that requires substantial editing does not meet the outcome.

### Revised milestones

| Milestone | Work | Exit condition |
| --- | --- | --- |
| M0: Establish baseline | Repair binary discovery and job accounting; time the current path with a representative stream; define examples of a good finished clip. | Reliable stage timings and a shared quality rubric; no performance claim yet. |
| M1: Automatic complete path | Combine phases 1, 2 and 4 with an initial selection/ranking pass: import, chunk analysis, shortlist, automatic layout/captions/audio, render and verify. | One real 3-hour source produces approximately 10 usable finished files without required manual steps. Record actual time even if over budget. |
| M2: Consistent editing quality | Apply phase 5 selection improvements and automatic edit templates; evaluate several different streams and failure cases. | Outputs consistently meet the upload-ready rubric; weak/duplicate clips and crop/timing failures are addressed. |
| M3: Reach the time target | Profile the slowest stages; apply phase 6 ASR, sampling, caching policy, encoding and concurrency experiments. Evaluate remote compute only where needed. | Demonstrate the approximately 20-minute target on at least three representative cold-cache runs with documented hardware/network/model settings and quality results. This is a proposed validation sample, not a statistical service guarantee. |
| M4: Optional controls/integrations | Add phase 3 corrections and phase 7 Resolve/MCP workflows. | These features improve exceptions and customization without becoming required for automatic completion. |

### Initial 20-minute engineering budget

These are proposed allocation ceilings to guide profiling, not measured timings. Tasks should overlap where safe; track the critical path. The allocations below total 20 minutes as an initial conservative budget.

| Work | Initial allocation |
| --- | --- |
| Import/download, probe and initialize analysis artifacts | 3 minutes |
| Coarse audio/video scan and chunked transcription | 8 minutes |
| Select, deduplicate and refine the best candidates; generate edit recipes | 3 minutes |
| Render the selected final clips | 5 minutes |
| Automated media checks and output packaging | 1 minute |

The overall compute target is approximately 9 times real-time relative to the source duration (180 minutes / 20 minutes). A longer transfer consumes the same total budget unless an alternative timing contract is explicitly selected. Large-file bandwidth, source resolution, model latency and final clip duration can materially change feasibility. Optimize coarse analysis across the full stream and reserve detailed video analysis/rendering for candidate intervals; processing every source frame with an expensive model is not the default plan.

### Updated decision register

| ID | Status | Decision | Rationale and affected work |
| --- | --- | --- | --- |
| GOAL-001 | Accepted (user requirement) | Target a 3-hour stream → roughly 10 strong, automatically edited, upload-ready clips in about 20 minutes. | Explicit user clarification; governs architecture, prioritization and evaluation. Achievement is unverified. |
| PLAN-006 | Supersedes PLAN-002 | Replace the editor-first delivery milestone with an automatic complete-path milestone. | Manual review would defeat the intended turnkey outcome. Phase 3 becomes optional correction work. |
| PLAN-007 | Proposed | Use coarse-to-fine analysis, automatic edit recipes, bounded concurrent tasks and final source-based renders. | Focus expensive work on candidate windows while preserving original quality. Validate with M1–M3. |
| PERF-002 | Proposed | Start with the stated stage budget and assess hardware/remote compute using measurements. | The 20-minute requirement is an engineering target, not an existing M4 capability claim. |
| DOC-003 | Implemented (documentation) | Update this plan, append the clarification to the review log, and record the governing target in repository instructions. | Preserve the corrected scope for subsequent ClipOS work; verify repository/vault synchronization. |


## Phase checkpoint — 2026-11-16: Phase 1 implemented; stopped for review

**Status: implemented and fixture/integration validated.** The user authorized
implementation one phase at a time and requested an update/stop after each phase.
Phase 2 has not started. See REL-001 through REL-007 in
[[2026-11-16-architecture-review]] for rationale, affected files and validation.

Completed phase 1's media-pair discovery/capability checks, additive migrations,
atomic single-active-run claim, persisted task IDs, per-stage/per-clip execution,
bounded transient retries/deadlines, heartbeats/reconciliation, safe cancellation,
verified checkpoint resume, partial clip preservation and detailed progress UI.
Automatic caption burning and refresh-safe output discovery remove the required
manual burn step. Existing API/schema/media compatibility is retained. Manual
stage changes are blocked only while automatic work is active.

Validation: **91 backend tests passed**, including private-Redis RQ failure and
actual workhorse-death/resume scenarios and real short-media operations;
**7 frontend tests passed** and the production build passed. Migrated a backed-up
copy of the default local library twice, preserving all 13 original jobs and
existing values. The live library and existing services were left untouched by
verification. Run API and worker with the updated code to apply migrations at
startup; worker.py enables the retry scheduler. No new dependencies were added.

**Milestone accounting:** M0's reliable execution and timing instrumentation are
implemented. Its representative-stream baseline and shared real-output quality
assessment remain pending. Synthetic/short fixtures do not establish M1's full
three-hour workflow, gaming clip quality or M3's approximately 20-minute target.
There was no full-stream inference/quality benchmark in this phase.

**Next package:** phase 2 (versioned edit recipes, proxies and one-pass rendering).
Do not begin it in the same implementation turn; stop here for the user's review.
The remaining delivery order stays 2 → 4 → 5 → 6, with phase 3 optional corrections
and phase 7 optional integrations.


## Phase checkpoint — 2026-11-16: Phase 2 implemented; stopped for review

**Status: implemented and short-media/integration validated.** The user authorized
starting the next phase after Phase 1. Phase 2's recipe, asset and source-rendering
work is implemented. See RECIPE-001, MEDIA-003, RENDER-001/002, LAYOUT-001,
EXPORT-001 and API-001 in [[2026-11-16-architecture-review]] for decisions, rationale,
affected files, validation and limitations. Phase 4 has not started.

Delivered versioned recipes with immutable revisions and conflict-safe saves;
source fingerprint/probe records; reusable proxy/waveform/thumbnail artifacts;
queued revision-pinned preview/final rendering; full-gameplay/background,
gameplay/facecam and positioned/keyframed crop compositions; one-pass final video
encoding directly from original media with ASS and selected-track audio mixing;
verified temporary-output promotion and durable available/current render history.
The automatic pipeline uses assets → transcription → highlights → recipe/source
render per clip → scoring. Gallery/playback/download views use the new final
outputs. Existing sources, legacy variants, canonical schemas and manual task
handlers remain compatible. The optional full editor is still phase 3.

Validation: **111 backend tests passed; 8 frontend tests and production build
passed.** Real media verifies layout geometry/aspect, rational frame rates, source
trim/concatenation order, visible caption timing, audio presence and A/V duration
alignment including delayed audio. Tests verify revision conflicts/history,
immutable source/output preservation, real private-Redis preview work, download
range requests and automatic caption/crop rerenders without repeating ASR or
selection. Migration 3 passed twice on a backed-up copy with all 13 jobs preserved.
The working library was not migrated during tests. Restart API/worker with the
updated code to activate the new tables and automatic path. No new dependencies.

**Milestone accounting:** this completes the phase-2 fixture acceptance checks,
not M1's representative three-hour automatic clip-quality result or M3's roughly
20-minute target. Full-source proxy preparation and ASR still need phase-4
chunking/resume and phase-6 profiling. Real gaming facecam composition, selection
quality, captions/pacing and total throughput remain to be evaluated. HDR/rotated
final inputs are explicitly unsupported until their planned conversion presets.

**Next package:** phase 4 — multi-hour source import, microphone/audio selection,
overlapping chunk analysis, checkpoint/cache identity and incremental candidates.
Stop here for the user's review. Do not start phase 4 in this implementation turn.


## Phase checkpoint — 2026-11-16: Phase 4 implemented; stopped for review

**Status: implementation and fixture acceptance delivered; representative-stream
acceptance pending.** The user authorized starting the next phase after Phase 2.
Phase 4 adds original-preserving library/MKV imports, queued remote downloads,
microphone selection, VAD and absolute timestamps, overlapping transcription/
selection chunks with durable caches, early candidates/rendering and explicit
storage maintenance. See INGEST-002, ASR-002, LONG-001, SELECT-002, CACHE-001,
RECIPE-002, MEDIA-004 and UX-002 in [[2026-11-16-architecture-review]].

Automatic execution now runs download (remote only) → bounded navigation sample →
repeated ASR/selection chunks and early candidate recipe/render units → final
scoring. Successful chunks are reused on resume; settings have separate ASR and
selection identities. Missing/corrupt canonical aggregates can be recovered from
valid chunks. Clip dependencies use their own words. Automatic captions can refresh
when boundary text changes while explicit human edits stay protected. The gallery
and details update during processing without requiring a manual review step.

Validation: **123 backend tests and 12 frontend tests passed**, production build,
lint (existing warnings) and whitespace checks passed. Real FFmpeg fixtures verify
MKV/MP4 track naming, microphone extraction, delayed audio, boundary timestamps,
source preservation and final rendering. A synthetic three-hour/36-section
scheduling test cancels/resumes without repeating successful ASR/selection units
and exposes the first clip before the second section. Inference, encoders and
queue transport are mocked in that long-timeline test; it is not a throughput or
clip-quality result. Provider/download failure, digest/cache invalidation,
count/duration/overlap budgets, protected cleanup and automatic-versus-human edit
behavior are covered. Migration 4/5 checks preserve all 13 original jobs on a
backed-up copy; the working library was not migrated for testing. No dependencies
were added. Restart API/worker to activate the new code/schema; set MEDIA_ROOT to
enable local-library browsing.

**Acceptance gap:** no representative 2–4-hour recording is currently available
in the library (11 readable sources; longest ~19 minutes). Full multi-hour ASR/
selection/render quality, real stop/resume under that workload, resource costs and
elapsed time remain unvalidated. The initial proxy/waveform is explicitly a 60s
navigation sample, not a full timeline proxy. Derived cache guards do not impose a
total quota on originals/exports; cleanup remains explicit and preserves edits.
M1's representative-stream result and the ~20-minute product target remain open.

**Next package:** Phase 5 — improve moment selection with audio/scene/visual
signals, rank before high-quality rendering and harden provider/response handling.
Phase 5 has not started. Stop here for the user's review. Phase 3 remains optional
manual corrections and Phase 7 optional integrations.


**Final verification addendum — 2026-11-16, implemented:** cache cleanup also
retains database-referenced originals and exports if a user imports/stores them
inside a cache directory. This closes a nested-path deletion risk without adding
a permission step or automatic eviction. Affected: `core/cache.py` and its source-
retention fixture. All 10 chunk/import/cache tests passed after this last guard;
the full 123-test backend suite and 12-test frontend suite, build/lint and
whitespace checks passed for the phase. Obsidian/repository copies were verified
identical after appending. Phase 5 remains untouched.


## Phase checkpoint — 2026-11-16: Phase 5 implemented; stopped for review

**Status: implemented; real-recording acceptance pending.** This phase changes the
automatic path to assets → complete ASR/candidate chunks → optional sampled visual
assessment → bounded global rank → selected recipe/final render. It deliberately
supersedes Phase 4's speculative early final rendering. Existing manual stage and
revision/render history remain optional. Canonical transcript/highlight keys and
the user's automatic three-hour → roughly ten upload-ready clips in twenty minutes
target remain intact; the latter is still a target, not a verified result.

Candidate windows now draw from transcript, optional audio peaks/scene transitions
and source-bound manual markers. A balanced shortlist retains nonverbal candidates.
Optional vision checks six sparse sampled frames and reports visible action,
reaction, context, payoff and uncertainty through a validated response. Rank
checks evidence and source intervals, applies count/duration/score budgets and
suppresses only strongly evidenced replays. No required human review was added.
The gallery and detail page expose reasons, candidate status and optional
Keep/Skip/Automatic feedback. Audio, scene and vision remain disabled by default
pending measured quality, cost and latency; chat evidence awaits a synchronized
source.

Ordered text/vision model tiers, request/chain deadlines, bounded transient
backoff, semantic validation and secret-safe errors are now centralized.
Migration 6 stores markers, optional feedback and per-call model telemetry. A
batch rank checkpoint prevents rerunning successful provider batches after a
later batch fails. Source, settings, marker and feedback changes invalidate
appropriate selection checkpoints while successful ASR chunks stay reusable.
Unknown provider cost and human review time are reported as unknown. The offline
paired evaluator accepts the same truth windows for both selection modes; its
included synthetic hand-authored example demonstrates the tool, not model quality.

**Affected files:** `app/config.py`, `app/migrations/__init__.py`, new selection
model/router and core signal/selection/validation/evaluation modules, chunk and
pipeline orchestration, LLM clients/fallback, frontend selection-review component,
API types and candidate/detail/progress views, tests, `.env.example`, README and
`SKILL.md`. Detailed decisions and rationale are in the matching architecture
review entry, mirrored to the Obsidian vault.

**Validation:** final full backend run: 147 tests passed; focused Phase 5 suite:
24 tests passed. Frontend: 15 tests
passed; production build and lint succeeded. Whitespace check passed. Real FFmpeg
fixture coverage includes audio/scene peaks and sampled frames; provider/rank
failure cases are mocked. The paired evaluator CLI produced the synthetic example
report. Migration 6 ran twice against a backup copy of the pre-existing library,
keeping all 13 jobs/values; the working library was untouched. No representative
2–4-hour recording, manually marked real comparison or end-to-end timing/quality
result was available, so acceptance/latency and the twenty-minute target remain
unverified. Live services were not restarted in this implementation check.

**Next package:** Phase 6 measured speed and output tuning, after the user asks
to continue. Stop here at the user's requested phase boundary.


## Phase checkpoint — 2026-11-17: Phase 6 local tuning implemented; stopped for review

**Status: implemented and tested on short local probes; three-hour acceptance
pending.** Phase 6 measured CPU/MLX ASR, libx264/VideoToolbox render, bounded
asset preparation and one ASR/render contention pair on this Apple M4 (16 GB).
Methods, exact results, memory/size and unknowns are in the dated
[`Phase 6 benchmark`](benchmarks/2026-11-17-phase6.md) and machine-readable JSON.
The source excerpts are 6–30 seconds from F1 broadcasts and 14.49 seconds of
known generated speech; they are not representative gaming-stream runs. CPU
faster-whisper and libx264 remain defaults because the alternatives did not
clearly improve this small quality/speed/size comparison. MLX is not a runtime
dependency. Current preview resolution and vision frame sampling were not changed
without marked gaming references.

The renderer now converts tagged BT.2020 PQ/HLG to BT.709 SDR before composing
clips, emits/verifies BT.709 tags for known SDR, preserves the H.264/AAC/yuv420p
export contract and rejects ambiguous HDR. Automatic recipes cap source rates
above 60 at 60 fps; synthetic VFR input rendered to an explicit 60 fps CFR final.
The renderer version change creates new immutable outputs and leaves old renders
in history. Short visual inspection of an F1 final showed the HUD and caption
within frame, with significant unused vertical space; this is not upload-ready
acceptance or a real HDR color review.

Pinned RQ/redis-py move to 2.6.1/7.1.0. On macOS TCP Redis, the worker uses
SpawnWorker and no longer preloads CV/ASR libraries in the parent; delayed retries
retain a scheduler. Private Redis confirmed a spawned job reached the finished
registry. An explicit Unix socket uses the prior fork worker because its spawn
trial failed; the scheduler can still fork and warn. A redis-py 8.1 trial also
failed spawn argument reconstruction and was not selected. Default worker/queue
count remains one pending realistic memory/throughput evidence.

**Validation:** final backend suite **153 passed**; private Redis/RQ integration,
real FFmpeg PQ final/HLG preview/VFR-to-60 tests, dependency check and whitespace
check passed. No frontend changes or paid API calls. No working-library migration,
source modification or live service restart. A representative three-hour gaming
recording, marked quality references, cold-cache end-to-end time, first usable
preview, real HDR appearance, whole-system peak resources and API cost were not
available. The roughly ten upload-ready clips in twenty minutes target remains
unverified.

**Affected files and decisions:** see the matching 2026-11-17 Phase 6 entry in
`2026-11-16-architecture-review.md`, mirrored with this checkpoint to the
Obsidian vault. Phase 7 remains optional and was not started. Stop here for the
user's requested phase review.
