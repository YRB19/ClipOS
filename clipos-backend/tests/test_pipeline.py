"""Tests for Phase 9: pipeline orchestration, jobs router, and worker."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.pipeline import run_pipeline


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
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _setup_job(client, db_engine, source_video, tmp_path, status="ingested"):
    """Ingest a video, return job_id with the given status."""
    with open(source_video, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("source.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    # Write highlights.json + transcript.json so pipeline doesn't error on file reads
    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "highlights.json").write_text(json.dumps(MOCK_HIGHLIGHTS))
    (raw_dir / "transcript.json").write_text(json.dumps([
        {"word": "hello", "start": 0.5, "end": 1.5},
        {"word": "world", "start": 1.5, "end": 2.5},
    ]))

    if status != "ingested":
        with Session(db_engine) as session:
            job = session.get(Job, job_id)
            job.status = status
            session.add(job)
            session.commit()

    return job_id


# ── run_pipeline unit tests ──────────────────────────────────────────────────


def test_run_pipeline_calls_stages_in_order(tmp_path):
    """Each pipeline stage must be called in the correct sequence."""
    mock_transcribe = MagicMock(return_value=[])
    mock_highlights = MagicMock(return_value=[])
    mock_cut = MagicMock(return_value=[
        {"clip_id": "clip-1", "clip_path": "/tmp/clip.mp4", "duration": 3.0},
    ])
    mock_reframe = MagicMock(return_value="/tmp/vertical.mp4")
    mock_captions = MagicMock(return_value="/tmp/captions.ass")
    mock_score = MagicMock(return_value=[])

    mock_engine = MagicMock()
    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "clips_ready"
    mock_session.get.return_value = mock_job
    mock_engine.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_engine.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.reframe_clip", mock_reframe), \
         patch("app.core.pipeline.generate_captions", mock_captions), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", mock_engine), \
         patch("app.core.pipeline._load_clip_ids", return_value=["clip-1"]):

        run_pipeline("test-job-123")

    # Verify call order via call_args_list
    assert mock_transcribe.call_args_list == [call("test-job-123")]
    assert mock_highlights.call_args_list == [call("test-job-123")]
    assert mock_cut.call_args_list == [call("test-job-123")]
    assert mock_reframe.call_args_list == [call("test-job-123", "clip-1")]
    assert mock_captions.call_args_list == [call("test-job-123", "clip-1")]
    assert mock_score.call_args_list == [call("test-job-123")]


def test_run_pipeline_failure_stops_chain(tmp_path):
    """A failure at any stage sets status=failed and skips all later stages."""
    mock_transcribe = MagicMock(side_effect=RuntimeError("whisper crashed"))
    mock_highlights = MagicMock()
    mock_cut = MagicMock()
    mock_reframe = MagicMock()
    mock_captions = MagicMock()
    mock_score = MagicMock()

    # Use a real in-memory DB so _fail() can actually update the Job row
    from sqlmodel import SQLModel as _SQLModel, Session as _Session
    from app.models.job import Job as _Job
    _eng = create_engine("sqlite:///:memory:")
    _SQLModel.metadata.create_all(_eng)
    with _Session(_eng) as _sess:
        _sess.add(_Job(job_id="test-job-fail", source_type="upload", status="ingested",
                       video_path="/tmp/fake.mp4", duration_seconds=10.0))
        _sess.commit()

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.reframe_clip", mock_reframe), \
         patch("app.core.pipeline.generate_captions", mock_captions), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", _eng):

        run_pipeline("test-job-fail")

    # Only transcribe was called — nothing after it
    mock_transcribe.assert_called_once()
    mock_highlights.assert_not_called()
    mock_cut.assert_not_called()
    mock_reframe.assert_not_called()
    mock_captions.assert_not_called()
    mock_score.assert_not_called()

    # Verify DB status
    with _Session(_eng) as _sess:
        job = _sess.get(_Job, "test-job-fail")
        assert job.status == "failed"
        assert "whisper crashed" in job.error_message


def test_run_pipeline_highlights_failure_stops_chain():
    """Failure in detect_highlights skips cut_clips and everything after."""
    mock_transcribe = MagicMock(return_value=[])
    mock_highlights = MagicMock(side_effect=RuntimeError("LLM all down"))
    mock_cut = MagicMock()
    mock_score = MagicMock()

    from sqlmodel import SQLModel as _SQLModel, Session as _Session
    from app.models.job import Job as _Job
    _eng = create_engine("sqlite:///:memory:")
    _SQLModel.metadata.create_all(_eng)
    with _Session(_eng) as _sess:
        _sess.add(_Job(job_id="test-job-hl-fail", source_type="upload", status="transcribed",
                       video_path="/tmp/fake.mp4", duration_seconds=10.0))
        _sess.commit()

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", _eng):

        run_pipeline("test-job-hl-fail")

    mock_transcribe.assert_called_once()
    mock_highlights.assert_called_once()
    mock_cut.assert_not_called()
    mock_score.assert_not_called()

    with _Session(_eng) as _sess:
        job = _sess.get(_Job, "test-job-hl-fail")
        assert job.status == "failed"
        assert "LLM all down" in job.error_message


def test_run_pipeline_clip_reframe_failure_stops():
    """A reframe failure on one clip stops the pipeline."""
    mock_transcribe = MagicMock(return_value=[])
    mock_highlights = MagicMock(return_value=[])
    mock_cut = MagicMock(return_value=[])
    mock_reframe = MagicMock(side_effect=RuntimeError("ffmpeg crash"))
    mock_captions = MagicMock()
    mock_score = MagicMock()

    from sqlmodel import SQLModel as _SQLModel, Session as _Session
    from app.models.job import Job as _Job
    _eng = create_engine("sqlite:///:memory:")
    _SQLModel.metadata.create_all(_eng)
    with _Session(_eng) as _sess:
        _sess.add(_Job(job_id="test-job-reframe-fail", source_type="upload", status="clips_ready",
                       video_path="/tmp/fake.mp4", duration_seconds=10.0))
        _sess.commit()

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.reframe_clip", mock_reframe), \
         patch("app.core.pipeline.generate_captions", mock_captions), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", _eng), \
         patch("app.core.pipeline._load_clip_ids", return_value=["clip-x"]):

        run_pipeline("test-job-reframe-fail")

    mock_reframe.assert_called_once()
    mock_captions.assert_not_called()
    mock_score.assert_not_called()

    with _Session(_eng) as _sess:
        job = _sess.get(_Job, "test-job-reframe-fail")
        assert job.status == "failed"
        assert "ffmpeg crash" in job.error_message


def test_run_pipeline_score_failure_sets_failed():
    """A failure in the final scoring stage sets status=failed."""
    mock_transcribe = MagicMock(return_value=[])
    mock_highlights = MagicMock(return_value=[])
    mock_cut = MagicMock(return_value=[])
    mock_reframe = MagicMock()
    mock_captions = MagicMock()
    mock_score = MagicMock(side_effect=RuntimeError("LLM rate limit"))

    from sqlmodel import SQLModel as _SQLModel, Session as _Session
    from app.models.job import Job as _Job
    _eng = create_engine("sqlite:///:memory:")
    _SQLModel.metadata.create_all(_eng)
    with _Session(_eng) as _sess:
        _sess.add(_Job(job_id="test-job-score-fail", source_type="upload", status="clips_ready",
                       video_path="/tmp/fake.mp4", duration_seconds=10.0))
        _sess.commit()

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.reframe_clip", mock_reframe), \
         patch("app.core.pipeline.generate_captions", mock_captions), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", _eng), \
         patch("app.core.pipeline._load_clip_ids", return_value=["clip-a"]):

        run_pipeline("test-job-score-fail")

    mock_score.assert_called_once()

    with _Session(_eng) as _sess:
        job = _sess.get(_Job, "test-job-score-fail")
        assert job.status == "failed"
        assert "LLM rate limit" in job.error_message


def test_run_pipeline_multiple_clips():
    """With 2 clips, reframe+captions are called twice (once per clip)."""
    mock_transcribe = MagicMock(return_value=[])
    mock_highlights = MagicMock(return_value=[])
    mock_cut = MagicMock(return_value=[
        {"clip_id": "c1", "clip_path": "/a.mp4", "duration": 3.0},
        {"clip_id": "c2", "clip_path": "/b.mp4", "duration": 4.0},
    ])
    mock_reframe = MagicMock(return_value="/v.mp4")
    mock_captions = MagicMock(return_value="/c.ass")
    mock_score = MagicMock(return_value=[])

    mock_engine = MagicMock()
    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_session.get.return_value = mock_job
    mock_engine.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_engine.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.core.pipeline.transcribe_video", mock_transcribe), \
         patch("app.core.pipeline.detect_highlights", mock_highlights), \
         patch("app.core.pipeline.cut_clips", mock_cut), \
         patch("app.core.pipeline.reframe_clip", mock_reframe), \
         patch("app.core.pipeline.generate_captions", mock_captions), \
         patch("app.core.pipeline.score_and_title_clips", mock_score), \
         patch("app.core.pipeline.engine", mock_engine), \
         patch("app.core.pipeline._load_clip_ids", return_value=["c1", "c2"]):

        run_pipeline("test-job-multi")

    assert mock_reframe.call_count == 2
    assert mock_captions.call_count == 2
    mock_score.assert_called_once()


# ── GET /jobs tests ──────────────────────────────────────────────────────────


def test_list_jobs(client, db_engine, source_video, tmp_path):
    """GET /jobs returns a list with correct shape, most recent first."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path)
    resp = client.get("/jobs")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    entry = data[0]
    assert "job_id" in entry
    assert "status" in entry
    assert "created_at" in entry
    assert "duration_seconds" in entry


def test_list_jobs_filter_by_status(client, db_engine, source_video, tmp_path):
    """GET /jobs?status=ingested filters correctly."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="ingested")
    resp = client.get("/jobs?status=ingested")

    assert resp.status_code == 200
    data = resp.json()
    assert all(j["status"] == "ingested" for j in data)
    assert any(j["job_id"] == job_id for j in data)


def test_list_jobs_empty_filter(client, db_engine, source_video, tmp_path):
    """GET /jobs?status=done returns empty list when no jobs are done."""
    _setup_job(client, db_engine, source_video, tmp_path, status="ingested")
    resp = client.get("/jobs?status=done")

    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /jobs/{job_id} tests ────────────────────────────────────────────────


def test_get_job_detail(client, db_engine, source_video, tmp_path):
    """GET /jobs/{job_id} returns full detail with clip_ids."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="clips_ready")
    resp = client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "clips_ready"
    assert "created_at" in data
    assert "duration_seconds" in data
    assert "clip_ids" in data  # highlights.json exists from _setup_job
    assert isinstance(data["clip_ids"], list)


def test_get_job_404(client, db_engine):
    """GET /jobs/{nonexistent} returns 404."""
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ── POST /jobs/{job_id}/run tests ───────────────────────────────────────────


def test_run_job_returns_409_wrong_status(client, db_engine, source_video, tmp_path):
    """POST /jobs/{id}/run returns 409 if status is not 'ingested'."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="transcribed")
    resp = client.post(f"/jobs/{job_id}/run")

    assert resp.status_code == 409
    assert "transcribed" in resp.json()["detail"]


def test_run_job_404(client, db_engine):
    """POST /jobs/{nonexistent}/run returns 404."""
    resp = client.post("/jobs/does-not-exist/run")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_run_job_enqueues(client, db_engine, source_video, tmp_path):
    """POST /jobs/{id}/run with status=ingested returns rq_job_id and status=queued."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="ingested")

    mock_rq_job = MagicMock()
    mock_rq_job.id = "rq-fake-123"

    with patch("app.routers.jobs.enqueue_pipeline", return_value="rq-fake-123"):
        resp = client.post(f"/jobs/{job_id}/run")

    assert resp.status_code == 200
    data = resp.json()
    assert data["rq_job_id"] == "rq-fake-123"
    assert data["status"] == "queued"


# ── GET /jobs/{job_id}/queue_status tests ────────────────────────────────────


def test_queue_status_returns_current_stage_from_db(client, db_engine, source_video, tmp_path):
    """queue_status.current_stage must come from jobs.status in the DB, not RQ state."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="transcribed")

    # Even with no RQ job found, current_stage should reflect the DB status
    resp = client.get(f"/jobs/{job_id}/queue_status")
    assert resp.status_code == 200
    data = resp.json()

    assert data["job_id"] == job_id
    assert data["current_stage"] == "transcribed"
    assert "rq_state" in data
    assert "error_message" in data


def test_queue_status_reflects_status_changes(client, db_engine, source_video, tmp_path):
    """queue_status tracks status changes in the DB across multiple reads."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="ingested")

    resp = client.get(f"/jobs/{job_id}/queue_status")
    assert resp.json()["current_stage"] == "ingested"

    # Simulate pipeline advancing the status
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = "highlights_ready"
        session.add(job)
        session.commit()

    resp = client.get(f"/jobs/{job_id}/queue_status")
    assert resp.json()["current_stage"] == "highlights_ready"


def test_queue_status_includes_error_message(client, db_engine, source_video, tmp_path):
    """queue_status.error_message is populated from the DB when set."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path, status="failed")

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.error_message = "transcribe: whisper crashed"
        session.add(job)
        session.commit()

    resp = client.get(f"/jobs/{job_id}/queue_status")
    data = resp.json()
    assert data["current_stage"] == "failed"
    assert data["error_message"] == "transcribe: whisper crashed"


def test_queue_status_404(client, db_engine):
    """queue_status returns 404 for nonexistent job."""
    resp = client.get("/jobs/nonexistent/queue_status")
    assert resp.status_code == 404


# ── _job_dict shape tests ───────────────────────────────────────────────────


def test_job_dict_includes_all_fields(client, db_engine, source_video, tmp_path):
    """GET /jobs returns all fields the frontend Job interface expects."""
    job_id = _setup_job(client, db_engine, source_video, tmp_path)
    resp = client.get("/jobs")
    entry = resp.json()[0]

    assert "job_id" in entry
    assert "source_filename" in entry
    assert "status" in entry
    assert "created_at" in entry
    assert "updated_at" in entry
    assert "duration_seconds" in entry
    assert "error_message" in entry
    assert entry["source_filename"] == "source.mp4"
