import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.transcribe import _call_whisper_asr


MOCK_TRANSCRIPT = [
    {"word": "this", "start": 0.0, "end": 0.28},
    {"word": "is", "start": 0.28, "end": 0.42},
    {"word": "a", "start": 0.42, "end": 0.48},
    {"word": "mocked", "start": 0.48, "end": 0.82},
    {"word": "transcript", "start": 0.82, "end": 1.36},
    {"word": "from", "start": 1.36, "end": 1.56},
    {"word": "whisper", "start": 1.56, "end": 1.94},
    {"word": "local", "start": 1.94, "end": 2.22},
    {"word": "asr", "start": 2.22, "end": 2.54},
]


def _make_mock_whisper_response(mock_words):
    """Build a mock faster-whisper transcribe response from our flat word list."""
    words = [SimpleNamespace(word=w["word"], start=w["start"], end=w["end"]) for w in mock_words]
    segment = SimpleNamespace(words=words)
    return [segment], None  # (segments, info)


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


@pytest.fixture(name="tiny_mp4")
def tiny_mp4(tmp_path):
    """Create a minimal valid mp4 file using ffmpeg."""
    path = tmp_path / "test.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3",
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


@pytest.fixture(name="ingested_job_id")
def ingested_job_id(client, tiny_mp4, db_engine):
    """Upload a file and return the job_id with status=ingested."""
    with open(tiny_mp4, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("test.mp4", f, "video/mp4")})
    return resp.json()["job_id"]


def _mock_whisper(mock_words):
    """Return a mock WhisperModel whose .transcribe() yields the given words."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = _make_mock_whisper_response(mock_words)
    return mock_model


def test_transcribe_success(client, ingested_job_id, db_engine, tmp_path):
    """Mock Whisper, verify transcript.json written and status updated."""
    raw_dir = Path(tmp_path) / "raw" / ingested_job_id

    real_raw = Path("./storage/raw") / ingested_job_id
    if real_raw.exists():
        import shutil
        raw_dir.mkdir(parents=True, exist_ok=True)
        for f in real_raw.iterdir():
            shutil.copy2(f, raw_dir / f.name)

    mock_model = _mock_whisper(MOCK_TRANSCRIPT)

    with patch("app.core.transcribe._get_model", return_value=mock_model):
        with patch("app.core.transcribe.engine", db_engine):
            with patch("app.core.transcribe.settings") as mock_transcribe_settings:
                mock_transcribe_settings.media_timeout_seconds = 900
                mock_transcribe_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.transcribe.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    resp = client.post(f"/jobs/{ingested_job_id}/transcribe")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == ingested_job_id
    assert data["word_count"] == len(MOCK_TRANSCRIPT)
    assert data["duration_covered"] == MOCK_TRANSCRIPT[-1]["end"]
    assert "transcript_path" in data

    # Verify transcript.json exists and has correct schema
    transcript_file = Path(data["transcript_path"])
    assert transcript_file.exists()
    transcript = json.loads(transcript_file.read_text())
    assert isinstance(transcript, list)
    for entry in transcript:
        assert "word" in entry
        assert "start" in entry
        assert "end" in entry
        assert isinstance(entry["word"], str)
        assert isinstance(entry["start"], (int, float))
        assert isinstance(entry["end"], (int, float))

    # Verify DB status updated
    with Session(db_engine) as session:
        job = session.get(Job, ingested_job_id)
        assert job.status == "transcribed"


def test_transcribe_status_transition(client, ingested_job_id, db_engine, tmp_path):
    """After transcription, status should be 'transcribed'."""
    real_raw = Path("./storage/raw") / ingested_job_id
    raw_dir = Path(tmp_path) / "raw" / ingested_job_id
    if real_raw.exists():
        import shutil
        raw_dir.mkdir(parents=True, exist_ok=True)
        for f in real_raw.iterdir():
            shutil.copy2(f, raw_dir / f.name)

    mock_model = _mock_whisper(MOCK_TRANSCRIPT)

    with patch("app.core.transcribe._get_model", return_value=mock_model):
        with patch("app.core.transcribe.engine", db_engine):
            with patch("app.core.transcribe.settings") as mock_transcribe_settings:
                mock_transcribe_settings.media_timeout_seconds = 900
                mock_transcribe_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.transcribe.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    client.post(f"/jobs/{ingested_job_id}/transcribe")

    with Session(db_engine) as session:
        job = session.get(Job, ingested_job_id)
        assert job.status == "transcribed"


def test_transcribe_404(client, db_engine):
    """Non-existent job_id returns 404."""
    with patch("app.core.transcribe.engine", db_engine):
        resp = client.post("/jobs/nonexistent-id/transcribe")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_transcribe_409(client, db_engine):
    """Job that isn't 'ingested' returns 409."""
    with Session(db_engine) as session:
        job = Job(
            job_id="test-409-job",
            source_type="upload",
            status="failed",
            video_path="/tmp/fake.mp4",
            duration_seconds=0.0,
            error_message="previous failure",
        )
        session.add(job)
        session.commit()

    with patch("app.core.transcribe.engine", db_engine):
        resp = client.post("/jobs/test-409-job/transcribe")
    assert resp.status_code == 409
    assert "failed" in resp.json()["detail"]


def test_whisper_returns_correct_schema():
    """The Whisper mock returns the expected transcript schema."""
    mock_model = _mock_whisper(MOCK_TRANSCRIPT)

    with patch("app.core.transcribe._get_model", return_value=mock_model):
        result = _call_whisper_asr("/fake/audio.wav")

    assert isinstance(result, list)
    for entry in result:
        assert set(entry.keys()) == {"word", "start", "end"}
        assert isinstance(entry["word"], str)
        assert isinstance(entry["start"], float)
        assert isinstance(entry["end"], float)
        assert entry["end"] >= entry["start"]
