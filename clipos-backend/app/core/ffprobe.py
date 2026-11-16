import json
import subprocess

from app.core.media import ffprobe


def get_video_metadata(path: str) -> dict:
    """Run ffprobe on a video file and return key metadata."""
    cmd = [
        ffprobe(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise ValueError("No video stream found in file")

    # fps from r_frame_rate like "30000/1001"
    fps_parts = video_stream.get("r_frame_rate", "0/1").split("/")
    fps = float(fps_parts[0]) / float(fps_parts[1]) if float(fps_parts[1]) != 0 else 0.0

    streams = data.get("streams", [])
    return {
        "duration": float(data["format"].get("duration", 0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": round(fps, 2),
        "codec": video_stream.get("codec_name", ""),
        "frame_rate": video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate", "0/1"),
        "r_frame_rate": video_stream.get("r_frame_rate", "0/1"),
        "sample_aspect_ratio": video_stream.get("sample_aspect_ratio", "1:1"),
        "color": {key: video_stream.get(key) for key in (
            "color_range", "color_space", "color_transfer", "color_primaries", "pix_fmt")},
        "audio_tracks": [{"stream_index": stream["index"], "codec": stream.get("codec_name"),
                          "channels": stream.get("channels"), "sample_rate": stream.get("sample_rate"),
                          "channel_layout": stream.get("channel_layout"),
                          "title": stream.get("tags", {}).get("title"),
                          "language": stream.get("tags", {}).get("language"),
                          "start_time": stream.get("start_time"), "duration": stream.get("duration")}
                         for stream in streams if stream.get("codec_type") == "audio"],
        "video_stream_index": video_stream.get("index", 0),
        "start_time": data["format"].get("start_time"),
        "streams": streams,
    }
