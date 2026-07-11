import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.main import app
from app.models.job import Job, get_session
from app.core.scoring import _extract_clip_text, _parse_scoring_response

MOCK_HIGHLIGHTS = [
    {
        "clip_id": "clip-aaa-111",
        "start": 0.0,
        "end": 5.0,
        "hook_text": "First hook",
        "virality_score": 0.9,
        "reasoning": "Strong opener",
    },
    {
        "clip_id": "clip-bbb-222",
        "start": 6.0,
        "end": 10.0,
        "hook_text": "Second hook",
        "virality_score": 0.7,
        "reasoning": "Good ending",
    },
]

MOCK_TRANSCRIPT = [
    {"word": "welcome", "start": 0.5, "end": 1.0},
    {"word": "everyone", "start": 1.0, "end": 1.5},
    {"word": "today", "start": 1.5, "end": 2.0},
    {"word": "we", "start": 2.0, "end": 2.2},
    {"word": "are", "start": 2.2, "end": 2.4},
    {"word": "going", "start": 2.4, "end": 2.6},
    {"word": "deep", "start": 2.6, "end": 3.0},
    {"word": "into", "start": 3.0, "end": 3.3},
    {"word": "this", "start": 3.3, "end": 3.5},
    {"word": "topic", "start": 3.5, "end": 4.0},
    {"word": "lets", "start": 6.5, "end": 7.0},
    {"word": "get", "start": 7.0, "end": 7.3},
    {"word": "started", "start": 7.3, "end": 7.8},
    {"word": "right", "start": 7.8, "end": 8.2},
    {"word": "away", "start": 8.2, "end": 8.6},
]

MOCK_SCORING_RESPONSE = json.dumps([
    {
        "clip_id": "clip-aaa-111",
        "title": "The Deep Dive Opener",
        "final_hook_text": "Welcome everyone — today we go deep",
        "final_score": 0.95,
        "rank_reasoning": "Strongest hook with immediate audience engagement",
    },
    {
        "clip_id": "clip-bbb-222",
        "title": "Let's Get Started",
        "final_hook_text": "Let's get started right away",
        "final_score": 0.82,
        "rank_reasoning": "Clean call-to-action but less emotionally gripping",
    },
])

MOCK_LLM_PATCH = "app.core.scoring.call_llm_with_fallback"
MOCK_ENGINE_PATCH = "app.core.scoring.engine"


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


def _setup_job(client, db_engine, tmp_path, status="clips_ready", highlights=None, transcript=None):
    """Create a job, write highlights.json + transcript.json, return job_id."""
    dummy = tmp_path / "dummy.mp4"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=12",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-c:a", "aac", str(dummy)],
        check=True, capture_output=True,
    )
    with open(dummy, "rb") as f:
        resp = client.post("/ingest/upload", files={"file": ("dummy.mp4", f, "video/mp4")})
    job_id = resp.json()["job_id"]

    raw_dir = Path("./storage/raw") / job_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "highlights.json").write_text(json.dumps(highlights or MOCK_HIGHLIGHTS))
    (raw_dir / "transcript.json").write_text(json.dumps(transcript or MOCK_TRANSCRIPT))

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        job.status = status
        session.add(job)
        session.commit()

    return job_id


def test_score_success(client, db_engine, tmp_path):
    """Mock LLM, verify highlights.json is merged and sorted by final_score."""
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, return_value=(MOCK_SCORING_RESPONSE, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 200
    scored = resp.json()

    assert len(scored) == 2
    assert scored[0]["clip_id"] == "clip-aaa-111"
    assert scored[0]["final_score"] == 0.95
    assert scored[0]["title"] == "The Deep Dive Opener"
    assert scored[1]["clip_id"] == "clip-bbb-222"
    assert scored[1]["final_score"] == 0.82

    highlights_path = Path("./storage/raw") / job_id / "highlights.json"
    on_disk = json.loads(highlights_path.read_text())
    assert len(on_disk) == 2
    assert on_disk[0]["final_score"] >= on_disk[1]["final_score"]


def test_merge_preserves_existing_fields(client, db_engine, tmp_path):
    """New fields are added; original fields are never removed or renamed."""
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, return_value=(MOCK_SCORING_RESPONSE, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 200
    for entry in resp.json():
        assert "clip_id" in entry
        assert "start" in entry
        assert "end" in entry
        assert "hook_text" in entry
        assert "virality_score" in entry
        assert "reasoning" in entry
        assert "title" in entry
        assert "final_hook_text" in entry
        assert "final_score" in entry
        assert "rank_reasoning" in entry


def test_sort_by_final_score(client, db_engine, tmp_path):
    """Highlights must be sorted descending by final_score."""
    swapped_response = json.dumps([
        {
            "clip_id": "clip-aaa-111",
            "title": "Opener",
            "final_hook_text": "Hook A",
            "final_score": 0.50,
            "rank_reasoning": "Lower",
        },
        {
            "clip_id": "clip-bbb-222",
            "title": "Closer",
            "final_hook_text": "Hook B",
            "final_score": 0.98,
            "rank_reasoning": "Higher",
        },
    ])
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, return_value=(swapped_response, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 200
    scored = resp.json()
    assert scored[0]["clip_id"] == "clip-bbb-222"
    assert scored[0]["final_score"] == 0.98
    assert scored[1]["clip_id"] == "clip-aaa-111"
    assert scored[1]["final_score"] == 0.50


def test_409_wrong_status(client, db_engine, tmp_path):
    """Scoring on a job not in 'clips_ready' returns 409."""
    job_id = _setup_job(client, db_engine, tmp_path, status="highlights_ready")

    resp = client.post(f"/jobs/{job_id}/score")
    assert resp.status_code == 409
    assert "highlights_ready" in resp.json()["detail"]


def test_409_transcribed_status(client, db_engine, tmp_path):
    """Scoring on a transcribed job returns 409."""
    job_id = _setup_job(client, db_engine, tmp_path, status="transcribed")

    resp = client.post(f"/jobs/{job_id}/score")
    assert resp.status_code == 409
    assert "transcribed" in resp.json()["detail"]


def test_404_nonexistent_job(client, db_engine):
    """Non-existent job_id returns 404."""
    resp = client.post("/jobs/nonexistent-id/score")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_malformed_llm_json_raises(client, db_engine, tmp_path):
    """Malformed LLM JSON should raise a clear error, not corrupt highlights.json."""
    job_id = _setup_job(client, db_engine, tmp_path)

    original_highlights = json.loads(
        (Path("./storage/raw") / job_id / "highlights.json").read_text()
    )

    with patch(MOCK_LLM_PATCH, return_value=("not json at all", "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 500
    assert "Scoring failed" in resp.json()["detail"]

    on_disk = json.loads(
        (Path("./storage/raw") / job_id / "highlights.json").read_text()
    )
    assert on_disk == original_highlights


def test_malformed_json_object_not_array_raises(client, db_engine, tmp_path):
    """LLM returning a JSON object instead of array should raise."""
    job_id = _setup_job(client, db_engine, tmp_path)

    bad_json = json.dumps({"clip_id": "something", "title": "nope"})

    with patch(MOCK_LLM_PATCH, return_value=(bad_json, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 500


def test_status_transitions_to_done(client, db_engine, tmp_path):
    """After successful scoring, job status should be 'done'."""
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, return_value=(MOCK_SCORING_RESPONSE, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            client.post(f"/jobs/{job_id}/score")

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "done"


def test_extract_clip_text():
    """_extract_clip_text should return words within the time window."""
    words = [
        {"word": "hello", "start": 1.0, "end": 1.5},
        {"word": "world", "start": 2.0, "end": 2.5},
        {"word": "foo", "start": 3.0, "end": 3.5},
    ]
    result = _extract_clip_text(words, 1.2, 2.3)
    assert "hello" in result
    assert "world" in result
    assert "foo" not in result


def test_parse_scoring_response_strips_fences():
    """Markdown fences around valid JSON should be stripped."""
    fenced = f"```json\n{MOCK_SCORING_RESPONSE}\n```"
    result = _parse_scoring_response(fenced)
    assert isinstance(result, list)
    assert len(result) == 2


def test_parse_scoring_response_raises_on_garbage():
    """Completely invalid JSON should raise."""
    with pytest.raises(json.JSONDecodeError):
        _parse_scoring_response("not json at all")


def test_missing_clip_id_in_llm_response_graceful(client, db_engine, tmp_path):
    """If LLM omits a clip_id, that clip keeps original fields with defaults for new ones."""
    partial_response = json.dumps([
        {
            "clip_id": "clip-aaa-111",
            "title": "The Opener",
            "final_hook_text": "Refined hook",
            "final_score": 0.90,
            "rank_reasoning": "Best clip",
        },
    ])
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, return_value=(partial_response, "tier_1:test")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 200
    scored = resp.json()
    assert len(scored) == 2

    aaa = next(e for e in scored if e["clip_id"] == "clip-aaa-111")
    assert aaa["title"] == "The Opener"

    bbb = next(e for e in scored if e["clip_id"] == "clip-bbb-222")
    assert bbb["title"] == ""
    assert bbb["final_hook_text"] == "Second hook"
    assert bbb["final_score"] == 0.7


def test_all_tiers_fail(client, db_engine, tmp_path):
    """When all LLM tiers fail, scoring returns 500."""
    job_id = _setup_job(client, db_engine, tmp_path)

    with patch(MOCK_LLM_PATCH, side_effect=RuntimeError("All LLM providers failed:\ntier_1: timeout")):
        with patch(MOCK_ENGINE_PATCH, db_engine):
            resp = client.post(f"/jobs/{job_id}/score")

    assert resp.status_code == 500
    assert "Scoring failed" in resp.json()["detail"]

    on_disk = json.loads(
        (Path("./storage/raw") / job_id / "highlights.json").read_text()
    )
    assert len(on_disk) == 2
    assert "title" not in on_disk[0]
