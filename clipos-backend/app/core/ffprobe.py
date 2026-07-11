import json
import shutil
import subprocess
from pathlib import Path

_FFMPEG_FULL = Path("/opt/homebrew/Cellar/ffmpeg-full/8.1.2_1/bin")
_FFPROBE_BIN = str(_FFMPEG_FULL / "ffprobe") if (_FFMPEG_FULL / "ffprobe").exists() else shutil.which("ffprobe")


def get_video_metadata(path: str) -> dict:
    """Run ffprobe on a video file and return key metadata."""
    cmd = [
        _FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise ValueError("No video stream found in file")

    # fps from r_frame_rate like "30000/1001"
    fps_parts = video_stream.get("r_frame_rate", "0/1").split("/")
    fps = float(fps_parts[0]) / float(fps_parts[1]) if float(fps_parts[1]) != 0 else 0.0

    return {
        "duration": float(data["format"].get("duration", 0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": round(fps, 2),
        "codec": video_stream.get("codec_name", ""),
    }
