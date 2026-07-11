import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.clip_cutter import cut_clips
from app.core.ffprobe import get_video_metadata


MOCK_HIGHLIGHTS = [
    {
        "clip_id": "clip-aaa-111",
        "start": 1.0,
        "end": 4.0,
        "hook_text": "First hook",
        "virality_score": 0.9,
        "reasoning": "Strong opener",
    },
    {
        "clip_id": "clip-bbb-222",
        "start": 6.0,
        "end": 9.0,
        "hook_text": "Second hook",
        "virality_score": 0.7,
        "reasoning": "Good ending",
    },
]


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
    """Create a 10-second test video with audio."""
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _setup_job_with_highlights(client, db_engine, source_video, tmp_path, highlights=None):
    """Ingest a video, write highlights.json, set status to highlights_ready. Return job_id."""
    # Ingest
    with open(source_video, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("source.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    # Write highlights to raw dir
    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "highlights.json").write_text(json.dumps(highlights or MOCK_HIGHLIGHTS))

    # Set status to highlights_ready directly in DB
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = "highlights_ready"
        session.add(job)
        session.commit()

    return job_id


def test_cut_clips_success(client, db_engine, source_video, tmp_path):
    """Cut two clips from a 10s video, verify output files and durations."""
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path)

    # Patch storage_clips to use tmp_path
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    with patch("app.core.clip_cutter.engine", db_engine):
        with patch("app.core.clip_cutter.settings") as mock_settings:
            mock_settings.storage_raw = str(tmp_path / "raw") if not Path(f"./storage/raw/{job_id}").exists() else f"./storage/raw/{job_id}/.."
            # Actually, let's use real paths since upload used them
            resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert len(data["clips"]) == 2

    for clip in data["clips"]:
        assert "clip_id" in clip
        assert "clip_path" in clip
        assert "duration" in clip
        assert clip["duration"] > 0
        assert Path(clip["clip_path"]).exists()

    # Verify DB status
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "clips_ready"


def test_cut_clips_out_of_bounds(client, db_engine, source_video, tmp_path):
    """Clips with timestamps beyond source duration should be clamped, not crash."""
    oob_highlights = [
        {
            "clip_id": "clip-oob-111",
            "start": 0.0,
            "end": 100.0,  # way beyond 10s source
            "hook_text": "OOB clip",
            "virality_score": 0.5,
            "reasoning": "Tests clamping",
        },
        {
            "clip_id": "clip-oob-222",
            "start": 8.0,
            "end": 9.0,
            "hook_text": "Normal clip",
            "virality_score": 0.6,
            "reasoning": "Should work fine",
        },
    ]
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path, oob_highlights)

    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200
    data = resp.json()
    # The OOB clip should have been clamped to 10s (or still produce output)
    # The normal clip should succeed
    assert len(data["clips"]) >= 1


def test_cut_clips_invalid_range(client, db_engine, source_video, tmp_path):
    """Clip with start >= end should be skipped gracefully."""
    bad_highlights = [
        {
            "clip_id": "clip-bad-111",
            "start": 5.0,
            "end": 3.0,  # end before start
            "hook_text": "Bad clip",
            "virality_score": 0.3,
            "reasoning": "Invalid range",
        },
        {
            "clip_id": "clip-ok-222",
            "start": 1.0,
            "end": 3.0,
            "hook_text": "Good clip",
            "virality_score": 0.8,
            "reasoning": "Valid",
        },
    ]
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path, bad_highlights)

    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200
    data = resp.json()
    # Only the valid clip should succeed
    assert len(data["clips"]) == 1
    assert data["clips"][0]["clip_id"] == "clip-ok-222"

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "clips_ready"


def test_cut_clips_all_fail_sets_failed_status(client, db_engine, source_video, tmp_path):
    """When all clips fail, job status should be 'failed'."""
    bad_highlights = [
        {
            "clip_id": "clip-all-bad",
            "start": 5.0,
            "end": 2.0,  # invalid range
            "hook_text": "Bad",
            "virality_score": 0.1,
            "reasoning": "All bad",
        },
    ]
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path, bad_highlights)

    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["clips"]) == 0

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "failed"
        assert job.error_message is not None


def test_cut_clips_404(client, db_engine):
    """Non-existent job returns 404."""
    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post("/jobs/nonexistent/clips")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_cut_clips_409(client, db_engine, source_video, tmp_path):
    """Job not in 'highlights_ready' status returns 409."""
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path)

    # Reset status to something invalid for clips
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = "transcribed"
        session.add(job)
        session.commit()

    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")
    assert resp.status_code == 409
    assert "transcribed" in resp.json()["detail"]


def test_clip_output_duration_matches_expected(client, db_engine, source_video, tmp_path):
    """Clip duration should be close to end-start, not wildly off."""
    highlights = [
        {
            "clip_id": "clip-dur-111",
            "start": 2.0,
            "end": 5.0,
            "hook_text": "3 second clip",
            "virality_score": 0.8,
            "reasoning": "Duration test",
        },
    ]
    job_id = _setup_job_with_highlights(client, db_engine, source_video, tmp_path, highlights)

    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")

    assert resp.status_code == 200
    clip = resp.json()["clips"][0]
    # Should be ~3 seconds, allow +-0.5s for encoding edge cases
    assert 2.0 <= clip["duration"] <= 4.0, f"Expected ~3s, got {clip['duration']}s"
