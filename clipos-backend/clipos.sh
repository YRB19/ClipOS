#!/usr/bin/env bash
# ClipOS dev CLI — wraps the API so you never hand-copy job_id/clip_id again.
# Usage: ./clipos.sh <command> [args]
#
# Commands:
#   upload <file>              Upload a video, sets it as the "current" job
#   transcribe [job_id]        Transcribe (defaults to current job)
#   highlights [job_id]        Run highlight detection (defaults to current job)
#   clips [job_id]             Cut clips (defaults to current job)
#   reframe [clip_id] [job_id] Reframe a clip (defaults to current job, first clip)
#   captions [clip_id] [job_id]  Generate ASS captions (defaults to current job/clip)
#   burn [clip_id] [job_id]    Burn captions into clip video (defaults to current job/clip)
#   run <file>                 Full pipeline: upload -> transcribe -> highlights -> clips
#   status [job_id]            Show current job/clip state
#   jobs                       List recent jobs (requires GET /jobs endpoint - see note below)
#   use <job_id>                Manually set the current job
#
# State is stored in .clipos_state (job_id, clip_ids) in the current directory.

set -euo pipefail

BASE_URL="${CLIPOS_BASE_URL:-http://localhost:8000}"
STATE_FILE=".clipos_state"

# --- helpers -----------------------------------------------------------

_jq() {
  if command -v jq >/dev/null 2>&1; then
    jq "$@"
  else
    python3 -c "
import sys, json
data = json.load(sys.stdin)
key = sys.argv[1].strip('.').replace('\"','')
print(data.get(key, ''))
" "$1"
  fi
}

_save_state() {
  local key="$1" val="$2"
  touch "$STATE_FILE"
  grep -v "^${key}=" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null || true
  mv "${STATE_FILE}.tmp" "$STATE_FILE" 2>/dev/null || true
  echo "${key}=${val}" >> "$STATE_FILE"
}

_get_state() {
  local key="$1"
  [ -f "$STATE_FILE" ] || { echo ""; return; }
  grep "^${key}=" "$STATE_FILE" | tail -1 | cut -d= -f2-
}

_current_job() {
  local job_id
  job_id=$(_get_state "job_id")
  if [ -z "$job_id" ]; then
    echo "No current job. Run './clipos.sh upload <file>' first, or pass a job_id explicitly." >&2
    exit 1
  fi
  echo "$job_id"
}

# --- commands ------------------------------------------------------------

cmd_upload() {
  local file="$1"
  echo "Uploading $file..."
  local resp
  resp=$(curl -s -X POST "$BASE_URL/ingest/upload" -F "file=@${file}")
  local job_id
  job_id=$(echo "$resp" | _jq -r '.job_id' 2>/dev/null || echo "$resp" | _jq '.job_id')
  if [ -z "$job_id" ] || [ "$job_id" = "null" ]; then
    echo "Upload failed: $resp" >&2
    exit 1
  fi
  _save_state "job_id" "$job_id"
  echo "job_id: $job_id  (saved as current job)"
}

cmd_transcribe() {
  local job_id="${1:-$(_current_job)}"
  echo "Transcribing $job_id..."
  curl -s -X POST "$BASE_URL/jobs/${job_id}/transcribe" -w "\n%{http_code}\n"
}

cmd_highlights() {
  local job_id="${1:-$(_current_job)}"
  echo "Detecting highlights for $job_id..."
  curl -s -X POST "$BASE_URL/jobs/${job_id}/highlights" -w "\n%{http_code}\n"
}

cmd_clips() {
  local job_id="${1:-$(_current_job)}"
  echo "Cutting clips for $job_id..."
  local resp
  resp=$(curl -s -X POST "$BASE_URL/jobs/${job_id}/clips")
  echo "$resp"
  # stash the first clip_id for convenience with reframe
  local first_clip
  first_clip=$(echo "$resp" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    clips = data.get('clips', [])
    print(clips[0]['clip_id'] if clips else '')
except Exception:
    print('')
")
  if [ -n "$first_clip" ]; then
    _save_state "clip_id" "$first_clip"
    echo "First clip_id: $first_clip (saved as current clip)"
  fi
}

cmd_reframe() {
  local clip_id="${1:-$(_get_state clip_id)}"
  local job_id="${2:-$(_current_job)}"
  if [ -z "$clip_id" ]; then
    echo "No clip_id available. Run './clipos.sh clips' first, or pass one explicitly." >&2
    exit 1
  fi
  echo "Reframing clip $clip_id (job $job_id)..."
  curl -s -X POST "$BASE_URL/jobs/${job_id}/clips/${clip_id}/reframe" -w "\n%{http_code}\n"
}

cmd_captions() {
  local clip_id="${1:-$(_get_state clip_id)}"
  local job_id="${2:-$(_current_job)}"
  if [ -z "$clip_id" ]; then
    echo "No clip_id available. Run './clipos.sh clips' first, or pass one explicitly." >&2
    exit 1
  fi
  echo "Generating captions for clip $clip_id (job $job_id)..."
  curl -s -X POST "$BASE_URL/jobs/${job_id}/clips/${clip_id}/captions" -w "\n%{http_code}\n"
}

cmd_burn() {
  local clip_id="${1:-$(_get_state clip_id)}"
  local job_id="${2:-$(_current_job)}"
  if [ -z "$clip_id" ]; then
    echo "No clip_id available. Run './clipos.sh clips' first, or pass one explicitly." >&2
    exit 1
  fi
  echo "Burning captions into clip $clip_id (job $job_id)..."
  curl -s -X POST "$BASE_URL/jobs/${job_id}/clips/${clip_id}/burn" -w "\n%{http_code}\n"
}

cmd_run() {
  local file="$1"
  cmd_upload "$file"
  local job_id
  job_id=$(_current_job)
  sleep 1
  cmd_transcribe "$job_id"
  sleep 1
  cmd_highlights "$job_id"
  sleep 1
  cmd_clips "$job_id"
  echo ""
  echo "Pipeline complete. job_id=$job_id"
}

cmd_full() {
  local file="$1"
  cmd_upload "$file"
  local job_id
  job_id=$(_current_job)
  sleep 1
  cmd_transcribe "$job_id"
  sleep 1
  cmd_highlights "$job_id"
  sleep 1
  cmd_clips "$job_id"
  sleep 1
  cmd_reframe
  sleep 1
  cmd_captions

  echo ""
  echo "Captions generated. Burn into the clip? [y/N]"
  read -r answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    sleep 1
    cmd_burn
    echo ""
    echo "=== Full pipeline complete (with burned captions) ==="
  else
    echo ""
    echo "=== Full pipeline complete (captions.ass saved, burn skipped) ==="
  fi
  cmd_status
}

cmd_status() {
  local job_id="${1:-$(_get_state job_id)}"
  local clip_id
  clip_id=$(_get_state clip_id)
  echo "Current job_id:  ${job_id:-none}"
  echo "Current clip_id: ${clip_id:-none}"
  echo "Base URL:        $BASE_URL"
}

cmd_use() {
  _save_state "job_id" "$1"
  echo "Current job set to $1"
}

cmd_jobs() {
  # NOTE: this requires a GET /jobs listing endpoint that doesn't exist yet.
  # See the note below the script — worth adding in Phase 7's prompt if useful.
  curl -s "$BASE_URL/jobs" -w "\n%{http_code}\n"
}

# --- dispatch --------------------------------------------------------------

case "${1:-}" in
  upload)     cmd_upload "$2" ;;
  transcribe) cmd_transcribe "${2:-}" ;;
  highlights) cmd_highlights "${2:-}" ;;
  clips)      cmd_clips "${2:-}" ;;
  reframe)    cmd_reframe "${2:-}" "${3:-}" ;;
  captions)   cmd_captions "${2:-}" "${3:-}" ;;
  burn)       cmd_burn "${2:-}" "${3:-}" ;;
  run)        cmd_run "$2" ;;
  full)       cmd_full "$2" ;;
  status)     cmd_status "${2:-}" ;;
  use)        cmd_use "$2" ;;
  jobs)       cmd_jobs ;;
  *)
    echo "Usage: ./clipos.sh {upload|transcribe|highlights|clips|reframe|captions|burn|run|full|status|use|jobs} [args]"
    exit 1
    ;;
esac