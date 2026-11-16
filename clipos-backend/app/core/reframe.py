import logging
import subprocess
from collections import deque
from pathlib import Path

import cv2

from app.config import settings
from app.core.media import ffmpeg
from app.core.ffprobe import get_video_metadata
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)

# Haar cascade bundled with OpenCV — reliable for frontal face detection
_HAAR_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
_SAMPLE_INTERVAL = 5  # detect face every Nth frame (denser = smoother panning)
_SMOOTH_WINDOW = 18  # moving average window for center-x smoothing
_KEYFRAME_INTERVAL = 1.0  # one ffmpeg expression keyframe per second


def _detect_faces_on_frames(video_path: str) -> list[tuple[int, int, int, int] | None]:
    """Sample frames from video, detect faces, return list of (x, y, w, h) or None per sampled frame.

    Applies CLAHE (contrast limited adaptive histogram equalization) to the
    grayscale frame before detection to improve reliability on low-light footage.
    Detection parameters are slightly relaxed (minNeighbors=3) to increase
    sensitivity — the moving-average smoothing downstream absorbs false positives.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    face_cascade = cv2.CascadeClassifier(_HAAR_PATH)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    frame_idx = 0
    detections = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % _SAMPLE_INTERVAL == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # CLAHE normalizes brightness/contrast per-tile, helping with
            # uneven lighting and low-light footage
            enhanced = clahe.apply(gray)
            faces = face_cascade.detectMultiScale(enhanced, scaleFactor=1.1, minNeighbors=3, minSize=(30, 30))
            if len(faces) > 0:
                # Pick the largest face
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                detections.append((int(x), int(y), int(w), int(h)))
            else:
                detections.append(None)
        frame_idx += 1

    cap.release()
    return detections


def _smooth_centers(detections: list[tuple[int, int, int, int] | None], source_width: int) -> list[float]:
    """Compute smoothed center-x sequence from face detections.

    Returns one value per sampled frame. Gaps are filled by carrying forward
    the last known good x-center. A moving-average window is applied across
    the full sequence for continuous smooth motion.
    """
    filled = []
    last_good = None
    for det in detections:
        if det is not None:
            last_good = det
        if last_good is not None:
            filled.append(last_good[0] + last_good[2] / 2)  # center-x
        else:
            filled.append(source_width / 2)

    if not filled:
        return []

    smoothed = []
    window = deque(maxlen=_SMOOTH_WINDOW)
    for cx in filled:
        window.append(cx)
        smoothed.append(sum(window) / len(window))

    return smoothed


def _compute_sample_times(n_samples: int, fps: float) -> list[float]:
    """Compute the wall-clock timestamp for each sampled frame."""
    return [i * _SAMPLE_INTERVAL / fps for i in range(n_samples)]


def _interpolate_to_keyframes(
    times: list[float],
    positions: list[float],
    interval: float = _KEYFRAME_INTERVAL,
) -> list[tuple[float, float]]:
    """Interpolate per-sample positions to fixed time intervals (linear interp).

    Produces one (time, x_position) pair per ``interval`` seconds, suitable for
    building a piecewise-linear ffmpeg expression.
    """
    if not times or not positions:
        return []

    duration = times[-1]
    keyframes = []
    t = 0.0

    while t <= duration:
        # Find the two nearest samples surrounding t
        idx = 0
        for i, st in enumerate(times):
            if st >= t:
                idx = i
                break
        else:
            idx = len(times) - 1

        if idx == 0:
            keyframes.append((t, positions[0]))
        else:
            t0, t1 = times[idx - 1], times[idx]
            p0, p1 = positions[idx - 1], positions[idx]
            if t1 > t0:
                frac = (t - t0) / (t1 - t0)
                keyframes.append((t, p0 + (p1 - p0) * frac))
            else:
                keyframes.append((t, p0))

        t += interval

    return keyframes


def _build_crop_expression(
    keyframes: list[tuple[float, float]],
    src_w: int,
    crop_w: int,
) -> str:
    """Build an ffmpeg crop x-expression from piecewise-linear keyframes.

    The returned expression references ``t`` (time in seconds) and uses nested
    ``if(between(...))`` to interpolate between keyframe positions.  The final
    value is clamped so the crop window never leaves the source frame.

    Built right-to-left so the outermost ``if`` covers the earliest time range.
    """
    max_x = src_w - crop_w

    if not keyframes:
        half = max_x / 2
        return f"clip({half:.2f}, 0, {max_x})"

    if len(keyframes) == 1:
        x = max(0.0, min(keyframes[0][1] - crop_w / 2, float(max_x)))
        return f"clip({x:.2f}, 0, {max_x})"

    # Start with the last keyframe (innermost fallback)
    last_t, last_x = keyframes[-1]
    expr = f"clip({last_x:.2f}-{crop_w / 2:.2f}, 0, {max_x})"

    # Build outward from right to left
    for i in range(len(keyframes) - 2, -1, -1):
        t0, x0 = keyframes[i]
        t1, x1 = keyframes[i + 1]

        if t1 > t0:
            inner = f"{x0:.2f}+({x1:.2f}-{x0:.2f})*(t-{t0:.4f})/({t1:.4f}-{t0:.4f})"
        else:
            inner = f"{x0:.2f}"

        expr = f"if(between(t,{t0:.4f},{t1:.4f}),clip({inner}-{crop_w / 2:.2f},0,{max_x}),{expr})"

    return expr


def reframe_clip(job_id: str, clip_id: str) -> str:
    """Reframe a landscape clip to 1080x1920 vertical using dynamic face tracking.

    NOTE: This is slower than the previous static-crop MVP because it samples
    every 5th frame (denser detection), computes a per-frame smoothed position
    sequence, and passes a time-varying crop expression to ffmpeg.  The
    trade-off is smooth panning motion that follows the speaker's face.
    """
    input_path = Path(settings.storage_clips) / job_id / clip_id / "clip.mp4"
    final_path = Path(settings.storage_clips) / job_id / clip_id / "clip_vertical.mp4"
    output_path = final_path.with_name("clip_vertical.partial.mp4")

    if not input_path.exists():
        raise FileNotFoundError(f"No clip.mp4 found at {input_path}")

    meta = get_video_metadata(str(input_path))
    src_w, src_h = meta["width"], meta["height"]
    fps = meta["fps"]

    # Compute fixed 9:16 crop dimensions
    crop_h = src_h
    crop_w = int(src_h * 9 / 16)
    if crop_w > src_w:
        crop_w = src_w

    try:
        detections = _detect_faces_on_frames(str(input_path))
        smoothed = _smooth_centers(detections, src_w)
        face_detected = any(d is not None for d in detections)
    except Exception as e:
        logger.warning("Face detection failed for %s/%s: %s — falling back to center crop", job_id, clip_id, e)
        detections = []
        smoothed = []
        face_detected = False

    # --- debug summary: always log what happened ---
    n_sampled = len(detections)
    n_with_face = sum(1 for d in detections if d is not None)
    n_gap_filled = n_sampled - n_with_face

    if n_sampled == 0:
        logger.info(
            "[reframe] %s/%s — 0 frames sampled (video too short or unopenable), "
            "using STATIC CENTER CROP",
            job_id, clip_id,
        )
    elif not face_detected:
        logger.info(
            "[reframe] %s/%s — %d frames sampled, 0 with face detected, "
            "%d gap-filled — using STATIC CENTER CROP (no faces anywhere)",
            job_id, clip_id, n_sampled, n_gap_filled,
        )
    else:
        logger.info(
            "[reframe] %s/%s — %d frames sampled, %d with face, "
            "%d gap-filled (carried forward last known good) — using DYNAMIC PANNING",
            job_id, clip_id, n_sampled, n_with_face, n_gap_filled,
        )

    if not face_detected or not smoothed:
        center_x = src_w / 2
        crop_x = int(center_x - crop_w / 2)
        crop_x = max(0, min(crop_x, src_w - crop_w))

        try:
            cmd = [
                ffmpeg(), "-y",
                "-i", str(input_path),
                "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:0,scale=1080:1920",
                "-c:v", "libx264", "-preset", "fast",
                "-c:a", "copy",
                str(output_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=settings.media_timeout_seconds)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"ffmpeg reframe failed: {e.stderr}") from e

        if not output_path.exists():
            raise RuntimeError("ffmpeg reframe completed but output file not found")

        if get_video_metadata(str(output_path))["duration"] <= 0:
            raise RuntimeError("Reframed output has zero duration")
        output_path.replace(final_path)
        return str(final_path)

    # Dynamic panning path: build time-varying crop expression
    times = _compute_sample_times(len(smoothed), fps)
    keyframes = _interpolate_to_keyframes(times, smoothed)
    crop_x_expr = _build_crop_expression(keyframes, src_w, crop_w)

    try:
        cmd = [
            ffmpeg(), "-y",
            "-i", str(input_path),
            "-vf", f"crop={crop_w}:{crop_h}:x='{crop_x_expr}':y=0,scale=1080:1920",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=settings.media_timeout_seconds)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg reframe failed: {e.stderr}") from e

    if not output_path.exists():
        raise RuntimeError("ffmpeg reframe completed but output file not found")

    if get_video_metadata(str(output_path))["duration"] <= 0:
        raise RuntimeError("Reframed output has zero duration")
    output_path.replace(final_path)
    return str(final_path)
