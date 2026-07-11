"""Caption generation and burn-in for ClipOS clips.

Generates word-level ASS subtitle files from transcript.json and highlights.json,
then burns them into clip video using ffmpeg's ASS filter.

Storage layout produced:
  storage/clips/{job_id}/{clip_id}/captions.ass
  storage/clips/{job_id}/{clip_id}/clip_captioned.mp4
"""

import json
import logging
import subprocess
from pathlib import Path

from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)

# --- ffmpeg binary with libass support ---
# Homebrew's default ffmpeg lacks --enable-libass.  ffmpeg-full has it.
import shutil
from pathlib import Path as _P

_FFMPEG_FULL = _P("/opt/homebrew/Cellar/ffmpeg-full/8.1.2_1/bin/ffmpeg")
_FFMPEG_BIN = str(_FFMPEG_FULL) if _FFMPEG_FULL.exists() else shutil.which("ffmpeg")

def _ffmpeg() -> str:
    """Return the path to an ffmpeg binary that supports the ASS filter."""
    return _FFMPEG_BIN

# --- ASS style constants (hardcoded, tune later) ---

FONT_NAME = "Arial"
FONT_SIZE = 90
PRIMARY_COLOUR = "&H00FFFFFF"  # white
HIGHLIGHT_COLOUR = "&H0000FFFF"  # bright yellow (ASS uses BBGGRR)
OUTLINE_COLOUR = "&H00000000"  # solid black
OUTLINE = 4
SHADOW = 0
BOLD = -1  # -1 = enabled in ASS
ALIGNMENT = 2  # numpad: bottom-center
MARGIN_V = 220  # vertical margin from bottom
PLAYRES_X = 1080
PLAYRES_Y = 1920

ASS_HEADER = """\
[Script Info]
Title: ClipOS Captions
ScriptType: v4.00+
PlayResX: {playres_x}
PlayResY: {playres_y}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{secondary},{outline_col},{back},{bold},0,0,0,100,100,0,0,1,{outline},{shadow},{align},40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format H:MM:SS.CC (centiseconds)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    cs = int((s - int(s)) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _load_transcript(raw_dir: Path) -> list[dict]:
    """Load word-level transcript from transcript.json."""
    path = raw_dir / "transcript.json"
    if not path.exists():
        raise FileNotFoundError(f"No transcript.json found at {path}")
    data = json.loads(path.read_text())
    if not data:
        raise ValueError(f"Transcript is empty at {path}")
    return data


def _load_clip_highlight(raw_dir: Path, clip_id: str) -> dict:
    """Load highlights.json and find the entry matching clip_id."""
    path = raw_dir / "highlights.json"
    if not path.exists():
        raise FileNotFoundError(f"No highlights.json found at {path}")
    highlights = json.loads(path.read_text())
    for h in highlights:
        if h.get("clip_id") == clip_id:
            return h
    raise ValueError(f"No highlight entry found for clip_id '{clip_id}'")


def _filter_and_rezero_words(
    words: list[dict], clip_start: float, clip_end: float
) -> list[dict]:
    """Filter transcript words to the clip's time range and re-zero timestamps."""
    filtered = []
    for w in words:
        w_start = w["start"]
        w_end = w["end"]
        # Include word if it overlaps with the clip range
        if w_end > clip_start and w_start < clip_end:
            filtered.append({
                "word": w["word"],
                "start": max(0.0, w_start - clip_start),
                "end": min(clip_end - clip_start, w_end - clip_start),
            })
    return filtered


def generate_captions(job_id: str, clip_id: str) -> str:
    """Generate a word-level ASS subtitle file for a clip.

    Reads transcript.json and highlights.json, filters words to the clip's
    time range, and produces an ASS file with one Dialogue line per word.
    Each word is highlighted in yellow at its scheduled time.

    Storage layout:
      storage/clips/{job_id}/{clip_id}/captions.ass
    """
    raw_dir = Path(settings.storage_raw) / job_id
    clip_dir = Path(settings.storage_clips) / job_id / clip_id
    output_path = clip_dir / "captions.ass"

    if not clip_dir.exists():
        raise FileNotFoundError(f"Clip directory not found at {clip_dir}")

    words = _load_transcript(raw_dir)
    highlight = _load_clip_highlight(raw_dir, clip_id)
    clip_start = highlight["start"]
    clip_end = highlight["end"]

    clip_words = _filter_and_rezero_words(words, clip_start, clip_end)

    if not clip_words:
        raise ValueError(
            f"No transcript words found within clip range [{clip_start}, {clip_end}]"
        )

    logger.info(
        "[captions] %s/%s — %d words in clip range [%.1f–%.1f]",
        job_id, clip_id, len(clip_words), clip_start, clip_end,
    )

    # Build ASS file
    header = ASS_HEADER.format(
        playres_x=PLAYRES_X,
        playres_y=PLAYRES_Y,
        font=FONT_NAME,
        size=FONT_SIZE,
        primary=PRIMARY_COLOUR,
        secondary=PRIMARY_COLOUR,
        outline_col=OUTLINE_COLOUR,
        back="&H80000000",
        bold=BOLD,
        outline=OUTLINE,
        shadow=SHADOW,
        align=ALIGNMENT,
        margin_v=MARGIN_V,
    )

    dialogue_lines = []
    for w in clip_words:
        start = _format_ass_time(w["start"])
        end = _format_ass_time(w["end"])
        # Each word highlighted in yellow via inline colour override
        text = f"{{\\c{HIGHLIGHT_COLOUR}}}{w['word']}{{\\c{PRIMARY_COLOUR}}}"
        dialogue_lines.append(
            f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
        )

    ass_content = header + "\n".join(dialogue_lines) + "\n"
    output_path.write_text(ass_content)

    logger.info(
        "[captions] %s/%s — saved %s (%d dialogue lines)",
        job_id, clip_id, output_path, len(dialogue_lines),
    )

    return str(output_path)


def burn_captions(job_id: str, clip_id: str) -> str:
    """Burn ASS captions into the clip video using ffmpeg's ASS filter.

    Prefers clip_vertical.mp4 (1080x1920) if it exists, otherwise falls back
    to clip.mp4.

    Storage layout produced:
      storage/clips/{job_id}/{clip_id}/clip_captioned.mp4
    """
    clip_dir = Path(settings.storage_clips) / job_id / clip_id
    captions_path = clip_dir / "captions.ass"
    output_path = clip_dir / "clip_captioned.mp4"

    if not captions_path.exists():
        raise FileNotFoundError(f"No captions.ass found at {captions_path}")

    # Prefer vertical (already reframed) if available
    input_path = clip_dir / "clip_vertical.mp4"
    if not input_path.exists():
        input_path = clip_dir / "clip.mp4"
    if not input_path.exists():
        raise FileNotFoundError(f"No input video found at {clip_dir}")

    # ffmpeg's ASS filter requires libass (ffmpeg-full on Homebrew macOS).
    captions_str = str(captions_path)
    if ":" in captions_str:
        captions_str = captions_str.replace("\\", "\\\\").replace(":", "\\:")
    vf = f"ass={captions_str}"

    try:
        cmd = [
            _ffmpeg(), "-y",
            "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg caption burn-in failed: {e.stderr}") from e

    if not output_path.exists():
        raise RuntimeError("ffmpeg caption burn-in completed but output file not found")

    # Verify output has nonzero duration
    meta = get_video_metadata(str(output_path))
    if meta["duration"] <= 0:
        raise RuntimeError(f"Captioned output has zero duration: {output_path}")

    logger.info(
        "[captions] %s/%s — burned captions → %s (%.1fs)",
        job_id, clip_id, output_path, meta["duration"],
    )

    return str(output_path)
