# Phase 7: optional Resolve handoff and ClipOS MCP

Status: implemented in ClipOS; Resolve import/render validation remains open.

## Source-linked handoff

`POST /jobs/{job_id}/resolve-export` accepts `{"clip_ids": ["..."]}` or an empty object for the full selected shortlist. It returns a folder with `timeline.otio`, `timeline.srt`, per-clip SRT files and `manifest.json`. The endpoint reads saved revisions if present, otherwise the default recipe, without creating a revision. The OTIO references the original file by absolute `file://` URI and puts each recipe interval on one video track plus a matching source audio track when audio exists. Requested seconds and floor/ceil source frame bounds are both in the manifest. ClipOS markers attached to the source fingerprint are included on matching OTIO clips. The export is staged, read back with OpenTimelineIO, and published only while the source stamp is unchanged. It does not alter the normal automatic render path.

In Resolve, import `timeline.otio` with **File → Import → Timeline**. Keep the original source at the manifest's `source_path`, or relink it through Resolve if moved. Import the matching SRT separately as subtitles. Inspect source in/out frames, especially for variable frame rate files. The manifest records recipe layout, audio track/gain, and output settings to rebuild manually. A current ClipOS final render is the fidelity-preserving flattened option if those effects must be exact.

| Item | Handoff behavior |
| --- | --- |
| Original source reference and ordered trims | OTIO video and default source audio references with frame-quantized ranges |
| Source markers | OTIO clip markers and source fingerprint in manifest; Resolve transfer unverified |
| Caption words | Separate SRT timed to the quantized timeline; style and animation must be rebuilt |
| Vertical layout, crop motion, facecam composition | Manifest only; rebuild in Resolve or use ClipOS flattened final |
| Selected audio streams, gain, mixing | Manifest only; rebuild in Resolve or use ClipOS flattened final |
| ClipOS render history and task state | Remain in ClipOS |

## ClipOS MCP

Install the optional server in its own Python environment because MCP 2.3.0 requires a Starlette version incompatible with the backend's FastAPI pin:

```sh
cd clipos-backend
python3.12 -m venv .mcp-venv
.mcp-venv/bin/python -m pip install -r requirements-mcp.txt
CLIPOS_MCP_JOB_ID=<job-uuid> CLIPOS_API_URL=http://127.0.0.1:8000 .mcp-venv/bin/python mcp_server.py
```

Configure the MCP client to launch that command over stdio. The job UUID is required and the server accepts only a local HTTP origin. Tools search moments, get job status, get/create a clip draft, save an edit with expected revision, request a preview/final render, poll durable render records, and export the Resolve handoff. Edits and renders go through the backend's existing validation, idle guard and queue. The render tool returns `render_id` and `rq_task_id` immediately; it does not wait for encoding.

## Validation on 2026-11-17

- OpenTimelineIO 0.18.1 wrote/read a two-interval fixture with original media URI, exact floor/ceil bounds and a marker; FastAPI served the OTIO/SRT. The fixture source hash stayed unchanged. These are interchange-file checks, not a Resolve import check.
- MCP 2.3.0 stdio client called the live isolated backend for a representative “find the comeback, change its caption, render it, and hand it to Resolve” sequence. A bad source fingerprint was rejected with API 422; a valid edit got a new revision; the render returned durable task IDs; the handoff returned an OTIO artifact. The test uses a mocked queue and does not establish completed rendering or model language understanding.
- Installed Resolve is 21.0.0.47. Its scripting API connected, but `CreateProject` returned no project and both `GetCurrentDatabase` and `GetDatabaseList` returned `None`. The desktop UI was unavailable to automation. Import, relink, marker transfer, frame alignment inside Resolve, and Resolve rendering remain unverified here.
- The official native AI assistant integration cited in the architecture review targets Resolve Studio 21.1; it cannot be evaluated on the installed 21.0 edition. No software purchase or upgrade was made.

The Phase 7 completion check remains open until an OTIO shortlist is imported, inspected, edited and rendered in a working Resolve project. This optional integration does not block the automatic ClipOS workflow.
