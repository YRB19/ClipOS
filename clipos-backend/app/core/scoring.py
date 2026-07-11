"""Scoring and titling pass for ClipOS clips.

Runs after clip cutting. Loads highlights.json + transcript.json,
asks the LLM to title, hook-refine, and re-score each clip in one
batched call, then merges the results back and overwrites highlights.json.
"""

import json
import logging
from pathlib import Path

from app.config import settings
from app.core.llm_fallback import call_llm_with_fallback
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a viral video strategist. You will receive a list of short-form video \
clips extracted from a longer video. Each clip includes its full spoken text.

For EACH clip, provide:
- "title": a short, punchy title (under 60 chars, suitable for a filename/thumbnail)
- "final_hook_text": a refined 1-line hook (you may reuse or improve the original hook_text)
- "final_score": a float 0-1, a re-scored virality estimate now that you see the full \
clip text — this may differ from the original virality_score
- "rank_reasoning": one sentence on why this clip ranks where it does relative to \
the others in this job

Return ONLY a JSON array (no prose, no markdown fences). \
Each element must have exactly these keys:
- "clip_id": matching the input clip_id
- "title": short title under 60 chars
- "final_hook_text": refined 1-line hook
- "final_score": float 0.0 to 1.0
- "rank_reasoning": one sentence

The array MUST be in the same order and have the same clip_ids as the input.\
"""


def _extract_clip_text(words: list[dict], start: float, end: float) -> str:
    """Extract the spoken text within a clip's [start, end] window."""
    clip_words = [
        w["word"] for w in words
        if w["end"] > start and w["start"] < end
    ]
    return " ".join(clip_words)


def _parse_scoring_response(raw: str) -> list[dict]:
    """Defensively parse JSON from LLM response, stripping markdown fences."""
    cleaned = raw.strip()
    cleaned = cleaned.strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    if cleaned.startswith("\n"):
        cleaned = cleaned[1:]

    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")
    return data


def score_and_title_clips(job_id: str) -> list[dict]:
    """Score and title all clips for a job in one LLM call.

    Loads highlights.json and transcript.json, builds a batched prompt,
    merges LLM results back into each highlight (additive fields only),
    sorts by final_score descending, overwrites highlights.json, and
    updates job status to "done".
    """
    raw_dir = Path(settings.storage_raw) / job_id
    highlights_path = raw_dir / "highlights.json"
    transcript_path = raw_dir / "transcript.json"

    if not highlights_path.exists():
        raise FileNotFoundError(f"No highlights.json found for job {job_id}")
    if not transcript_path.exists():
        raise FileNotFoundError(f"No transcript.json found for job {job_id}")

    highlights = json.loads(highlights_path.read_text())
    if not highlights:
        raise ValueError(f"highlights.json is empty for job {job_id}")

    transcript = json.loads(transcript_path.read_text())
    if not transcript:
        raise ValueError(f"transcript.json is empty for job {job_id}")

    # Build batched prompt: each candidate with its spoken text
    candidate_blocks = []
    for h in highlights:
        clip_text = _extract_clip_text(transcript, h["start"], h["end"])
        candidate_blocks.append(
            f'clip_id: {h["clip_id"]}\n'
            f'timestamps: {h["start"]:.1f}s - {h["end"]:.1f}s\n'
            f'original_hook_text: {h["hook_text"]}\n'
            f'original_virality_score: {h["virality_score"]}\n'
            f'spoken_text: "{clip_text}"'
        )

    user_content = (
        "Here are the clips to score and title:\n\n"
        + "\n\n".join(candidate_blocks)
        + "\n\nReturn your response as a JSON array, one object per clip, "
        "in the same order with the same clip_ids."
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw_content, _provider_used = call_llm_with_fallback(
        messages=messages,
        temperature=0.3,
        max_tokens=4096,
    )

    scoring_results = _parse_scoring_response(raw_content)

    # Index LLM results by clip_id for merging
    scoring_by_id = {r["clip_id"]: r for r in scoring_results if "clip_id" in r}

    # Merge: additive only — keep all existing fields, add new ones
    merged = []
    for h in highlights:
        entry = dict(h)  # copy, preserve all original fields
        llm_data = scoring_by_id.get(h["clip_id"], {})
        if not llm_data:
            logger.warning(
                "LLM response missing clip_id %s — keeping original fields only",
                h["clip_id"],
            )
        entry["title"] = llm_data.get("title", "")
        entry["final_hook_text"] = llm_data.get("final_hook_text", h.get("hook_text", ""))
        entry["final_score"] = float(llm_data.get("final_score", h.get("virality_score", 0.0)))
        entry["rank_reasoning"] = llm_data.get("rank_reasoning", "")
        merged.append(entry)

    # Sort by final_score descending
    merged.sort(key=lambda x: x["final_score"], reverse=True)

    # Overwrite highlights.json
    highlights_path.write_text(json.dumps(merged, indent=2))

    # Update job status to "done"
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            job.status = "done"
            session.add(job)
            session.commit()

    return merged
