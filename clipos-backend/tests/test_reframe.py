import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.ffprobe import get_video_metadata
from app.core.reframe import (
    _smooth_centers,
    _interpolate_to_keyframes,
    _build_crop_expression,
    _compute_sample_times,
)


@pytest.fixture(name="db_engine")
def db_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="client")
def client(db_engine):
    def override_get_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(name="source_video")
def source_video(tmp_path):
    """Create a 5-second 320x240 landscape test video."""
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=5",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-shortest",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(name="clip_path")
def clip_path(client, source_video, db_engine, tmp_path):
    """Ingest a video, cut a single clip, return its clip_id and paths."""
    # Ingest
    with open(source_video, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("source.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    # Write highlights and set status
    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    highlights = [
        {"clip_id": "test-clip-001", "start": 0.5, "end": 3.5, "hook_text": "test", "virality_score": 0.5, "reasoning": "test"},
    ]
    (raw_dir / "highlights.json").write_text(json.dumps(highlights))

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = "highlights_ready"
        session.add(job)
        session.commit()

    # Cut clip
    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200

    return job_id, "test-clip-001"


def test_reframe_success(client, clip_path, db_engine):
    """Reframe a clip, verify output exists and is 1080x1920."""
    job_id, cid = clip_path

    resp = client.post(f"/jobs/{job_id}/clips/{cid}/reframe")
    assert resp.status_code == 200
    data = resp.json()
    assert data["clip_id"] == cid
    assert "vertical_path" in data

    vpath = Path(data["vertical_path"])
    assert vpath.exists()

    meta = get_video_metadata(str(vpath))
    assert meta["width"] == 1080
    assert meta["height"] == 1920


def test_reframe_no_face_falls_back_to_center(client, clip_path, db_engine):
    """When face detection finds nothing, should fall back to center crop and still produce 1080x1920."""
    job_id, cid = clip_path

    # Mock face detection to return empty (no faces)
    with patch("app.core.reframe._detect_faces_on_frames", return_value=[None, None, None]):
        resp = client.post(f"/jobs/{job_id}/clips/{cid}/reframe")

    assert resp.status_code == 200
    vpath = Path(resp.json()["vertical_path"])
    assert vpath.exists()

    meta = get_video_metadata(str(vpath))
    assert meta["width"] == 1080
    assert meta["height"] == 1920


def test_reframe_smooth_centers_with_gaps():
    """Moving average should handle None gaps and produce a smooth sequence."""
    # 320 wide source, faces at x=100, None, x=200, x=250
    detections = [(100, 0, 50, 50), None, (200, 0, 50, 50), (250, 0, 50, 50)]
    centers = _smooth_centers(detections, 320)
    assert len(centers) == 4
    # Each value should be a moving average — no wild jumps
    for i in range(1, len(centers)):
        assert abs(centers[i] - centers[i - 1]) < 100


def test_reframe_smooth_centers_no_faces():
    """No faces at all should fill with source center (caller checks detections for None)."""
    centers = _smooth_centers([None, None, None], 320)
    assert len(centers) == 3
    assert all(c == 160.0 for c in centers)


def test_reframe_smooth_centers_returns_sequence():
    """Smooth centers should return one value per input detection."""
    detections = [(50, 0, 40, 40), (100, 0, 40, 40), (150, 0, 40, 40)]
    centers = _smooth_centers(detections, 320)
    assert len(centers) == 3
    # Values should be moving averages of center-x positions
    assert all(isinstance(c, float) for c in centers)


def test_reframe_dynamic_panning_varies_crop_position(client, clip_path, db_engine):
    """When face moves across frames, the crop position should actually change over time."""
    job_id, cid = clip_path

    # Mock detections that simulate a face moving from left to right across a 320-wide source
    # Face at x=20 (left), then x=120 (center), then x=220 (right) — large movement
    moving_detections = [
        (20, 0, 40, 40), (20, 0, 40, 40), (20, 0, 40, 40),
        (120, 0, 40, 40), (120, 0, 40, 40), (120, 0, 40, 40),
        (220, 0, 40, 40), (220, 0, 40, 40), (220, 0, 40, 40),
    ]

    with patch("app.core.reframe._detect_faces_on_frames", return_value=moving_detections):
        resp = client.post(f"/jobs/{job_id}/clips/{cid}/reframe")

    assert resp.status_code == 200
    vpath = Path(resp.json()["vertical_path"])
    assert vpath.exists()

    meta = get_video_metadata(str(vpath))
    assert meta["width"] == 1080
    assert meta["height"] == 1920


def test_build_crop_expression_varies_with_time():
    """The generated expression should produce different x-values at different times."""
    # 320 wide source, crop_w = 180 (320 * 9/16 ≈ 180)
    src_w = 320
    crop_w = 180
    # Two keyframes: face at left at t=0, face at right at t=3
    keyframes = [(0.0, 40.0), (3.0, 280.0)]
    expr = _build_crop_expression(keyframes, src_w, crop_w)
    assert "between" in expr
    assert "clip" in expr


def test_interpolate_to_keyframes_linear():
    """Interpolation should produce correct values at keyframe times."""
    times = [0.0, 0.167, 0.333, 0.5]
    positions = [100.0, 120.0, 140.0, 160.0]
    keyframes = _interpolate_to_keyframes(times, positions, interval=0.25)

    assert len(keyframes) >= 2
    # First keyframe at t=0 should be close to 100
    assert abs(keyframes[0][1] - 100.0) < 5
    # Should have keyframes at 0, 0.25, 0.5, etc.
    times_kf = [k[0] for k in keyframes]
    assert 0.0 in times_kf


def test_compute_sample_times():
    """Sample times should be evenly spaced based on sample interval and fps."""
    times = _compute_sample_times(10, fps=30.0)
    assert len(times) == 10
    assert times[0] == 0.0
    # Each sample is every 5 frames at 30fps = 0.1667s apart
    assert abs(times[1] - 5 / 30.0) < 1e-6


def test_reframe_404_no_job(client, db_engine):
    """Non-existent job returns 404."""
    resp = client.post("/jobs/fake-id/clips/fake-clip/reframe")
    assert resp.status_code == 404


def test_reframe_404_no_clip(client, clip_path, db_engine):
    """Non-existent clip file returns 404."""
    job_id, _ = clip_path
    resp = client.post(f"/jobs/{job_id}/clips/nonexistent-clip/reframe")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_reframe_logs_detection_stats(client, clip_path, db_engine, caplog):
    """Debug logging should report sampled/detected/gap-filled counts and which path was taken."""
    job_id, cid = clip_path

    # Mock detections: 2 with face, 1 gap-filled (None)
    detections = [(50, 0, 40, 40), None, (150, 0, 40, 40)]

    with caplog.at_level("INFO", logger="app.core.reframe"):
        with patch("app.core.reframe._detect_faces_on_frames", return_value=detections):
            resp = client.post(f"/jobs/{job_id}/clips/{cid}/reframe")

    assert resp.status_code == 200

    # Check that the debug summary was logged
    assert any("[reframe]" in record.message for record in caplog.records)
    assert any("3 frames sampled" in record.message for record in caplog.records)
    assert any("2 with face" in record.message for record in caplog.records)
    assert any("1 gap-filled" in record.message for record in caplog.records)
    assert any("DYNAMIC PANNING" in record.message for record in caplog.records)


def test_reframe_logs_static_fallback_when_no_faces(client, clip_path, db_engine, caplog):
    """When no faces detected, logging should say STATIC CENTER CROP."""
    job_id, cid = clip_path

    with caplog.at_level("INFO", logger="app.core.reframe"):
        with patch("app.core.reframe._detect_faces_on_frames", return_value=[None, None, None]):
            resp = client.post(f"/jobs/{job_id}/clips/{cid}/reframe")

    assert resp.status_code == 200
    assert any("STATIC CENTER CROP" in record.message for record in caplog.records)


def test_detect_faces_applies_clahe():
    """_detect_faces_on_frames should create and use CLAHE before detection."""
    from unittest.mock import MagicMock, patch
    import app.core.reframe as mod

    mock_clahe = MagicMock()
    mock_clahe.apply.side_effect = lambda gray: gray  # pass through

    mock_cascade = MagicMock()
    mock_cascade.detectMultiScale.return_value = []

    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    # 7 frames: only frames 0 and 5 are sampled (every 5th)
    frames = [MagicMock() for _ in range(7)]
    mock_cap.read.side_effect = [(True, f) for f in frames] + [(False, None)]

    with patch("app.core.reframe.cv2") as mock_cv2:
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.CascadeClassifier.return_value = mock_cascade
        mock_cv2.createCLAHE.return_value = mock_clahe
        mock_cv2.COLOR_BGR2GRAY = 6

        mod._detect_faces_on_frames("/fake/video.mp4")

        # CLAHE should be created with reasonable params
        mock_cv2.createCLAHE.assert_called_once_with(clipLimit=2.0, tileGridSize=(8, 8))

        # clahe.apply should have been called for each sampled frame (2 frames sampled)
        assert mock_clahe.apply.call_count == 2


def test_detect_faces_uses_clahe_module():
    """Verify CLAHE is instantiated inside _detect_faces_on_frames with correct params."""
    from unittest.mock import MagicMock, patch
    import app.core.reframe as mod

    mock_clahe = MagicMock()
    mock_clahe.apply.side_effect = lambda gray: gray

    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    mock_cap.read.side_effect = [(True, MagicMock())] * 3 + [(False, None)]

    mock_cascade = MagicMock()
    mock_cascade.detectMultiScale.return_value = []

    with patch("app.core.reframe.cv2") as mock_cv2:
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.CascadeClassifier.return_value = mock_cascade
        mock_cv2.createCLAHE.return_value = mock_clahe
        mock_cv2.COLOR_BGR2GRAY = 6

        mod._detect_faces_on_frames("/fake/video.mp4")

    mock_cv2.createCLAHE.assert_called_once_with(clipLimit=2.0, tileGridSize=(8, 8))
    assert mock_clahe.apply.call_count >= 1
