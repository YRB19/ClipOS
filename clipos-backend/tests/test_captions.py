import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.captions import (
    _format_ass_time,
    _filter_and_rezero_words,
    generate_captions,
    burn_captions,
    ASS_HEADER,
    HIGHLIGHT_COLOUR,
    PRIMARY_COLOUR,
)

MOCK_TRANSCRIPT = [
    {"word": "hello", "start": 0.5, "end": 1.0},
    {"word": "this", "start": 1.1, "end": 1.4},
    {"word": "is", "start": 1.5, "end": 1.7},
    {"word": "a", "start": 1.8, "end": 1.9},
    {"word": "test", "start": 2.0, "end": 2.5},
    {"word": "of", "start": 3.0, "end": 3.2},
    {"word": "captions", "start": 3.3, "end": 4.0},
    {"word": "beyond", "start": 5.0, "end": 5.5},
    {"word": "range", "start": 5.6, "end": 6.0},
]

MOCK_HIGHLIGHTS = [
    {
        "clip_id": "clip-alpha",
        "start": 0.5,
        "end": 4.0,
        "hook_text": "test hook",
        "virality_score": 0.8,
        "reasoning": "test",
    },
    {
        "clip_id": "clip-beta",
        "start": 3.0,
        "end": 5.0,
        "hook_text": "second clip",
        "virality_score": 0.7,
        "reasoning": "test",
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


@pytest.fixture(name="clip_with_highlights")
def clip_with_highlights(client, source_video, db_engine, tmp_path):
    """Ingest, cut clip, write transcript + highlights, return job_id and clip_id."""
    with open(source_video, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("source.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))
    (raw_dir / "highlights.json").write_text(json.dumps(MOCK_HIGHLIGHTS))

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = "highlights_ready"
        session.add(job)
        session.commit()

    # Cut clip
    with patch("app.core.clip_cutter.engine", db_engine):
        resp = client.post(f"/jobs/{job_id}/clips")
    assert resp.status_code == 200

    return job_id, "clip-alpha"


# --- Unit tests for helpers ---


def test_format_ass_time():
    """ASS time format should be H:MM:SS.CC."""
    assert _format_ass_time(0.0) == "0:00:00.00"
    assert _format_ass_time(65.5) == "0:01:05.50"
    assert _format_ass_time(3600.0) == "1:00:00.00"
    assert _format_ass_time(61.25) == "0:01:01.25"


def test_filter_and_rezero_words():
    """Words overlapping clip range should be re-zeroed; others filtered out."""
    words = [
        {"word": "before", "start": 1.0, "end": 2.0},
        {"word": "inside", "start": 5.5, "end": 6.0},
        {"word": "straddle", "start": 9.5, "end": 10.5},  # overlaps end
        {"word": "after", "start": 12.0, "end": 13.0},
    ]
    result = _filter_and_rezero_words(words, clip_start=5.0, clip_end=10.0)
    assert len(result) == 2
    assert result[0]["word"] == "inside"
    assert result[0]["start"] == pytest.approx(0.5)
    assert result[0]["end"] == pytest.approx(1.0)
    assert result[1]["word"] == "straddle"
    assert result[1]["start"] == pytest.approx(4.5)
    assert result[1]["end"] == pytest.approx(5.0)  # clamped to clip duration


def test_filter_and_rezero_words_no_overlap():
    """If no words overlap, return empty list."""
    words = [{"word": "x", "start": 20.0, "end": 21.0}]
    result = _filter_and_rezero_words(words, clip_start=5.0, clip_end=10.0)
    assert result == []


# --- Integration tests via API ---


def test_generate_captions_success(client, clip_with_highlights, db_engine):
    """Generate captions, verify .ass file is written with correct structure."""
    job_id, cid = clip_with_highlights

    resp = client.post(f"/jobs/{job_id}/clips/{cid}/captions")
    assert resp.status_code == 200
    data = resp.json()
    assert "captions_path" in data

    ass_path = Path(data["captions_path"])
    assert ass_path.exists()
    assert ass_path.suffix == ".ass"

    content = ass_path.read_text()
    assert "[Script Info]" in content
    assert "[V4+ Styles]" in content
    assert "[Events]" in content
    assert "Dialogue:" in content
    # Words within clip range [5.0, 10.0]: hello, this, is, a, test, of, captions
    assert "hello" in content
    assert "captions" in content
    # Words outside clip range should NOT appear
    assert "beyond" not in content


def test_generate_captions_highlights_word_colour(client, clip_with_highlights, db_engine):
    """Each word Dialogue line should have yellow highlight colour override."""
    job_id, cid = clip_with_highlights

    resp = client.post(f"/jobs/{job_id}/clips/{cid}/captions")
    assert resp.status_code == 200

    content = Path(resp.json()["captions_path"]).read_text()
    dialogue_lines = [l for l in content.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogue_lines) > 0

    for line in dialogue_lines:
        # Each line should have the yellow highlight override
        assert HIGHLIGHT_COLOUR in line
        # And restore to primary after the word
        assert PRIMARY_COLOUR in line


def test_generate_captions_404_no_job(client, db_engine):
    resp = client.post("/jobs/fake-id/clips/fake-clip/captions")
    assert resp.status_code == 404


def test_generate_captions_404_no_clip(client, clip_with_highlights, db_engine):
    job_id, _ = clip_with_highlights
    resp = client.post(f"/jobs/{job_id}/clips/nonexistent/captions")
    assert resp.status_code == 404


def test_burn_captions_success(client, clip_with_highlights, db_engine):
    """Burn captions into clip, verify output exists with valid duration."""
    job_id, cid = clip_with_highlights

    # First generate captions
    resp = client.post(f"/jobs/{job_id}/clips/{cid}/captions")
    assert resp.status_code == 200

    clip_dir = Path(f"./storage/clips/{job_id}/{cid}")

    # Use a side_effect that only intercepts ffmpeg (not ffprobe) calls
    import shutil
    real_run = subprocess.run

    def selective_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "ffmpeg":
            # Simulate ffmpeg: copy input to output
            input_file = cmd[cmd.index("-i") + 1]
            output_file = cmd[-1]
            shutil.copy2(input_file, output_file)
            return MagicMock(returncode=0, stdout="", stderr="")
        return real_run(cmd, **kwargs)

    with patch("app.core.captions.subprocess.run", side_effect=selective_run):
        resp = client.post(f"/jobs/{job_id}/clips/{cid}/burn")

    assert resp.status_code == 200
    data = resp.json()
    assert "captioned_clip_path" in data
    assert Path(data["captioned_clip_path"]).exists()


def test_burn_captions_404_no_job(client, db_engine):
    resp = client.post("/jobs/fake-id/clips/fake-clip/burn")
    assert resp.status_code == 404


def test_burn_captions_404_no_clip(client, clip_with_highlights, db_engine):
    job_id, _ = clip_with_highlights
    resp = client.post(f"/jobs/{job_id}/clips/nonexistent/burn")
    assert resp.status_code == 404


def test_burn_captions_no_captions_file(client, clip_with_highlights, db_engine):
    """Burn without generating captions first should return 500."""
    job_id, cid = clip_with_highlights

    resp = client.post(f"/jobs/{job_id}/clips/{cid}/burn")
    assert resp.status_code == 500
    assert "captions.ass" in resp.json()["detail"]


def test_ass_header_has_correct_resolution():
    """ASS header should specify 1080x1920 playres."""
    header = ASS_HEADER.format(
        playres_x=1080, playres_y=1920,
        font="Arial", size=90,
        primary="&H00FFFFFF", secondary="&H00FFFFFF",
        outline_col="&H00000000", back="&H80000000",
        bold=-1, outline=4, shadow=0, align=2, margin_v=220,
    )
    assert "PlayResX: 1080" in header
    assert "PlayResY: 1920" in header
