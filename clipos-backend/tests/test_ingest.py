import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.ffprobe import get_video_metadata


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
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
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


def test_upload_success(client, tiny_mp4):
    with open(tiny_mp4, "rb") as f:
        response = client.post(
            "/ingest/upload",
            files={"file": ("test.mp4", f, "video/mp4")},
        )
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert len(data["job_id"]) == 36  # uuid4 format


def test_upload_invalid_file_type(client, tmp_path):
    bad_file = tmp_path / "test.txt"
    bad_file.write_text("not a video")
    with open(bad_file, "rb") as f:
        response = client.post(
            "/ingest/upload",
            files={"file": ("test.txt", f, "text/plain")},
        )
    assert response.status_code == 400
    assert "Invalid file type" in response.json()["detail"]


def test_get_video_metadata(tiny_mp4):
    meta = get_video_metadata(str(tiny_mp4))
    assert meta["duration"] > 0
    assert meta["width"] == 320
    assert meta["height"] == 240
    assert meta["fps"] > 0
    assert meta["codec"] == "h264"
