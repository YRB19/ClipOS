import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.highlights import _parse_llm_json, _validate_highlights, _build_transcript_text


MOCK_LLM_RESPONSE = json.dumps([
    {
        "start": 12.3,
        "end": 47.8,
        "hook_text": "This one trick changed everything",
        "virality_score": 0.92,
        "reasoning": "Strong hook with curiosity gap, ideal for short-form",
    },
    {
        "start": 102.0,
        "end": 138.5,
        "hook_text": "Nobody talks about this enough",
        "virality_score": 0.85,
        "reasoning": "Emotional peak with relatable insight",
    },
    {
        "start": 200.0,
        "end": 245.0,
        "hook_text": "The moment it all clicked",
        "virality_score": 0.78,
        "reasoning": "Complete narrative arc in under 50 seconds",
    },
])

MOCK_TRANSCRIPT = [
    {"word": "this", "start": 0.0, "end": 0.28},
    {"word": "is", "start": 0.28, "end": 0.42},
    {"word": "a", "start": 0.42, "end": 0.48},
    {"word": "test", "start": 0.48, "end": 0.72},
    {"word": "transcript", "start": 0.72, "end": 1.26},
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


@pytest.fixture(name="tiny_mp4")
def tiny_mp4(tmp_path):
    path = tmp_path / "test.mp4"
    import subprocess
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


@pytest.fixture(name="transcribed_job_id")
def transcribed_job_id(client, tiny_mp4, db_engine, tmp_path):
    """Upload, transcribe, return job_id with status=transcribed."""
    with open(tiny_mp4, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("test.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    # Write mock transcript to the real storage location
    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    # Patch and transcribe to get status to "transcribed"
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (
        [SimpleNamespace(words=[SimpleNamespace(word=w["word"], start=w["start"], end=w["end"]) for w in MOCK_TRANSCRIPT])],
        None,
    )
    with patch("app.core.transcribe._get_model", return_value=mock_model):
        with patch("app.core.transcribe.engine", db_engine):
            with patch("app.core.transcribe.settings") as ts:
                ts.media_timeout_seconds = 900
                ts.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.transcribe.settings") as rs:
                    rs.storage_raw = str(tmp_path / "raw")
                    # Copy source.mp4 to tmp_path raw dir
                    tmp_raw = Path(tmp_path / "raw") / job_id
                    tmp_raw.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(raw_dir / "source.mp4", tmp_raw / "source.mp4")
                    shutil.copy2(raw_dir / "transcript.json", tmp_raw / "transcript.json")
                    client.post(f"/jobs/{job_id}/transcribe")

    # Update status directly in DB since we patched the engine during transcribe
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        if job:
            job.status = "transcribed"
            # Selection responses use a synthetic 300-second timeline; media
            # operations remain on the tiny fixture and do not benchmark ASR.
            job.duration_seconds = 300
            session.add(job)
            session.commit()

    return job_id


def test_highlights_success(client, transcribed_job_id, db_engine, tmp_path):
    """Mock LLM, verify highlights.json written in canonical schema."""
    # Ensure transcript.json exists in the location highlights.py will read
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.highlights.engine", db_engine):
            with patch("app.core.highlights.settings") as mock_settings:
                mock_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.highlights.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == transcribed_job_id
    assert data["candidate_count"] == 3
    assert "highlights_path" in data

    # Verify highlights.json schema
    highlights_file = Path(data["highlights_path"])
    assert highlights_file.exists()
    highlights = json.loads(highlights_file.read_text())
    assert isinstance(highlights, list)
    assert len(highlights) == 3
    for h in highlights:
        assert set(h.keys()) == {"clip_id", "start", "end", "hook_text", "virality_score", "reasoning"}
        assert isinstance(h["clip_id"], str) and len(h["clip_id"]) == 36
        assert isinstance(h["start"], float)
        assert isinstance(h["end"], float)
        assert isinstance(h["hook_text"], str)
        assert isinstance(h["virality_score"], float) and 0.0 <= h["virality_score"] <= 1.0
        assert isinstance(h["reasoning"], str)
        assert h["end"] > h["start"]

    # Verify DB status
    with Session(db_engine) as session:
        job = session.get(Job, transcribed_job_id)
        assert job.status == "highlights_ready"


def test_highlights_malformed_json(client, transcribed_job_id, db_engine, tmp_path):
    """LLM returns markdown-fenced garbage — should still parse or fail gracefully."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    # Response wrapped in markdown fences
    fenced = f"```json\n{MOCK_LLM_RESPONSE}\n```"
    llm_return = {"choices": [{"message": {"content": fenced}}]}

    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.highlights.engine", db_engine):
            with patch("app.core.highlights.settings") as mock_settings:
                mock_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.highlights.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    assert resp.json()["candidate_count"] == 3


def test_highlights_llm_returns_truly_broken_json(client, transcribed_job_id, db_engine, tmp_path):
    """LLM returns completely unparseable text — should fail with 500 and set status=failed."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": "Sorry, I can't do that."}}]}

    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.highlights.engine", db_engine):
            with patch("app.core.highlights.settings") as mock_settings:
                mock_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.highlights.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 500
    with Session(db_engine) as session:
        job = session.get(Job, transcribed_job_id)
        assert job.status == "failed"
        assert job.error_message is not None


def test_highlights_status_transition(client, transcribed_job_id, db_engine, tmp_path):
    """After successful highlights, status should be 'highlights_ready'."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.highlights.engine", db_engine):
            with patch("app.core.highlights.settings") as mock_settings:
                mock_settings.storage_raw = str(tmp_path / "raw")
                with patch("app.routers.highlights.settings") as mock_router_settings:
                    mock_router_settings.storage_raw = str(tmp_path / "raw")
                    client.post(f"/jobs/{transcribed_job_id}/highlights")

    with Session(db_engine) as session:
        job = session.get(Job, transcribed_job_id)
        assert job.status == "highlights_ready"


def test_highlights_404(client, db_engine):
    """Non-existent job_id returns 404."""
    with patch("app.core.highlights.engine", db_engine):
        resp = client.post("/jobs/nonexistent-id/highlights")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_highlights_409(client, db_engine):
    """Job that isn't 'transcribed' returns 409."""
    with Session(db_engine) as session:
        job = Job(
            job_id="test-409-hl",
            source_type="upload",
            status="ingested",
            video_path="/tmp/fake.mp4",
            duration_seconds=10.0,
        )
        session.add(job)
        session.commit()

    with patch("app.core.highlights.engine", db_engine):
        resp = client.post("/jobs/test-409-hl/highlights")
    assert resp.status_code == 409
    assert "ingested" in resp.json()["detail"]


def test_build_transcript_text():
    """Verify transcript text reconstruction produces timestamped segments."""
    text = _build_transcript_text(MOCK_TRANSCRIPT)
    assert "[0.00-" in text
    assert "this is a test transcript" in text


def test_parse_llm_json_strips_fences():
    """Markdown fences around valid JSON should be stripped."""
    fenced = '```json\n[{"start": 1.0, "end": 2.0, "hook_text": "hi", "virality_score": 0.5, "reasoning": "test"}]\n```'
    result = _parse_llm_json(fenced)
    assert isinstance(result, list)
    assert len(result) == 1


def test_parse_llm_json_raises_on_garbage():
    """Completely invalid JSON should raise ValueError."""
    with pytest.raises(ValueError):
        _parse_llm_json("not json at all")


def test_validate_highlights_filters_bad_entries():
    """Entries missing required keys should be filtered out."""
    good = {"start": 0.0, "end": 5.0, "hook_text": "hi", "virality_score": 0.5, "reasoning": "ok"}
    bad = {"start": 0.0}
    result = _validate_highlights([good, bad, "string"])
    assert len(result) == 1
    assert "clip_id" in result[0]


def test_openrouter_fallback_triggers_on_nvidia_failure(client, transcribed_job_id, db_engine, tmp_path):
    """When all NIM models fail, fallback to OpenRouter should succeed with identical output."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    with patch("app.core.nvidia_client.nvidia_chat_completion", side_effect=ConnectionError("NVIDIA timeout")):
        with patch("app.core.openrouter_client.openrouter_chat_completion", return_value=llm_return):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as mock_settings:
                    mock_settings.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as mock_router_settings:
                        mock_router_settings.storage_raw = str(tmp_path / "raw")
                        resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    data = resp.json()
    assert data["candidate_count"] == 3

    # Verify highlights.json written with correct schema
    highlights_file = Path(data["highlights_path"])
    assert highlights_file.exists()
    highlights = json.loads(highlights_file.read_text())
    assert len(highlights) == 3
    for h in highlights:
        assert set(h.keys()) == {"clip_id", "start", "end", "hook_text", "virality_score", "reasoning"}


def test_tier1_success_skips_tier2_and_tier3(client, transcribed_job_id, db_engine, tmp_path):
    """When tier 1 (GLM-5.2 on NIM) succeeds, tier 2 and tier 3 should not be called."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    mock_openrouter = MagicMock()

    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.openrouter_client.openrouter_chat_completion", mock_openrouter):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as mock_settings:
                    mock_settings.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as mock_router_settings:
                        mock_router_settings.storage_raw = str(tmp_path / "raw")
                        resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    mock_openrouter.assert_not_called()


def test_tier1_failure_falls_to_tier2(client, transcribed_job_id, db_engine, tmp_path):
    """When tier 1 fails, tier 2 (Mistral NIM) should be called and succeed."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    # nvidia_chat_completion called twice: first for GLM (tier1, fails), then for Mistral (tier2, succeeds)
    mock_nvidia = MagicMock(side_effect=[ConnectionError("GLM down"), llm_return])
    mock_openrouter = MagicMock()

    with patch("app.core.nvidia_client.nvidia_chat_completion", mock_nvidia):
        with patch("app.core.openrouter_client.openrouter_chat_completion", mock_openrouter):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as mock_settings:
                    mock_settings.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as mock_router_settings:
                        mock_router_settings.storage_raw = str(tmp_path / "raw")
                        resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    assert resp.json()["candidate_count"] == 3
    mock_nvidia.assert_called()
    mock_openrouter.assert_not_called()


def test_tier1_and_tier2_failure_falls_to_tier3(client, transcribed_job_id, db_engine, tmp_path):
    """When tiers 1 and 2 both fail, tier 3 (OpenRouter) should be called and succeed."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    mock_nvidia = MagicMock(side_effect=[ConnectionError("GLM down"), TimeoutError("Mistral timeout")])
    mock_openrouter = MagicMock(return_value=llm_return)

    with patch("app.core.nvidia_client.nvidia_chat_completion", mock_nvidia):
        with patch("app.core.openrouter_client.openrouter_chat_completion", mock_openrouter):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as mock_settings:
                    mock_settings.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as mock_router_settings:
                        mock_router_settings.storage_raw = str(tmp_path / "raw")
                        resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 200
    assert resp.json()["candidate_count"] == 3
    mock_nvidia.assert_called()
    mock_openrouter.assert_called_once()


def test_all_tiers_fail_raises_combined_error(client, transcribed_job_id, db_engine, tmp_path):
    """When all 3 tiers fail, the error should include all failure reasons and set status=failed."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    with patch("app.core.nvidia_client.nvidia_chat_completion", side_effect=ConnectionError("GLM down")):
        with patch("app.core.openrouter_client.openrouter_chat_completion", side_effect=TimeoutError("OpenRouter timeout")):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as mock_settings:
                    mock_settings.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as mock_router_settings:
                        mock_router_settings.storage_raw = str(tmp_path / "raw")
                        resp = client.post(f"/jobs/{transcribed_job_id}/highlights")

    assert resp.status_code == 500
    with Session(db_engine) as session:
        job = session.get(Job, transcribed_job_id)
        assert job.status == "failed"
        assert job.error_message is not None
        assert "ConnectionError" in job.error_message
        assert "GLM down" not in job.error_message
        assert "TimeoutError" in job.error_message


def test_identical_output_regardless_of_provider(client, transcribed_job_id, db_engine, tmp_path):
    """Highlights.json output should be identical regardless of which tier serves it."""
    raw_dir = Path(tmp_path / "raw") / transcribed_job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcript.json").write_text(json.dumps(MOCK_TRANSCRIPT))

    llm_return = {"choices": [{"message": {"content": MOCK_LLM_RESPONSE}}]}

    # Run with tier 1 (GLM-5.2)
    with patch("app.core.nvidia_client.nvidia_chat_completion", return_value=llm_return):
        with patch("app.core.openrouter_client.openrouter_chat_completion"):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as ms:
                    ms.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as rs:
                        rs.storage_raw = str(tmp_path / "raw")
                        resp_tier1 = client.post(f"/jobs/{transcribed_job_id}/highlights")
                        tier1_highlights = json.loads(
                            Path(resp_tier1.json()["highlights_path"]).read_text()
                        )

    # Reset job status for second run
    with Session(db_engine) as session:
        job = session.get(Job, transcribed_job_id)
        job.status = "transcribed"
        session.add(job)
        session.commit()

    # Run with tier 2 (Mistral NIM — tier 1 fails, tier 2 succeeds)
    mock_nvidia = MagicMock(side_effect=[ConnectionError("GLM down"), llm_return])
    with patch("app.core.nvidia_client.nvidia_chat_completion", mock_nvidia):
        with patch("app.core.openrouter_client.openrouter_chat_completion"):
            with patch("app.core.highlights.engine", db_engine):
                with patch("app.core.highlights.settings") as ms:
                    ms.storage_raw = str(tmp_path / "raw")
                    with patch("app.routers.highlights.settings") as rs:
                        rs.storage_raw = str(tmp_path / "raw")
                        resp_tier2 = client.post(f"/jobs/{transcribed_job_id}/highlights")
                        tier2_highlights = json.loads(
                            Path(resp_tier2.json()["highlights_path"]).read_text()
                        )

    # Compare content fields (clip_id is regenerated each time, so skip it)
    for t1, t2 in zip(tier1_highlights, tier2_highlights):
        assert t1["start"] == t2["start"]
        assert t1["end"] == t2["end"]
        assert t1["hook_text"] == t2["hook_text"]
        assert t1["virality_score"] == t2["virality_score"]
        assert t1["reasoning"] == t2["reasoning"]
