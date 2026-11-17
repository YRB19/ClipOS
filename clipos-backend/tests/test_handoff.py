import hashlib
import json
import subprocess
import os
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock

import opentimelineio as otio
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import settings
from app.core.media import ffmpeg
from app.core.recipes import EditRecipe, Interval, ensure_edit, save_edit
from app.models.job import Job
from app.models.selection import MomentMarker
from app.main import app


@pytest.fixture
def handoff_source(tmp_path):
    from app.models.job import engine
    job_id, clip_id = str(uuid4()), str(uuid4())
    raw = Path(settings.storage_raw)/job_id
    raw.mkdir(parents=True)
    path = raw/'source.mp4'
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=s=320x180:r=30000/1001:d=4', '-f', 'lavfi', '-i',
        'sine=frequency=440:duration=4', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'aac', str(path)], check=True, capture_output=True, timeout=30)
    (raw/'highlights.json').write_text(json.dumps([dict(clip_id=clip_id, start=.7, end=2.5,
        hook_text='Boss fight', reasoning='Loud comeback', virality_score=.8)]))
    (raw/'transcript.json').write_text(json.dumps([dict(word='comeback', start=.9, end=1.2)]))
    with Session(engine) as session:
        session.add(Job(job_id=job_id, source_type='upload', status='done',
                        video_path=str(path), duration_seconds=4))
        session.commit()
    return engine, job_id, clip_id, path


def test_otio_handoff_preserves_source_ranges_markers_and_caption_alignment(handoff_source):
    engine, job_id, clip_id, path = handoff_source
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    edit = ensure_edit(job_id, clip_id)
    recipe = EditRecipe.model_validate(edit['recipe'])
    recipe.intervals = [Interval(start=.7, end=1.4), Interval(start=2, end=2.5)]
    recipe.captions.words = [recipe.captions.words[0]]
    saved = save_edit(job_id, clip_id, recipe, edit['revision'])
    from app.core.assets import ensure_source
    fingerprint = ensure_source(job_id).fingerprint
    with Session(engine) as session:
        session.add(MomentMarker(job_id=job_id, source_fingerprint=fingerprint,
                                 time_seconds=1.0, label='Payoff'))
        session.commit()
    with TestClient(app) as client:
        search = client.get(f'/jobs/{job_id}/moments/search', params={'q': 'comeBACK'})
        assert search.status_code == 200 and search.json()[0]['clip_id'] == clip_id
        assert client.get(f'/jobs/{job_id}/moments/search', params={'q': 'not present'}).json() == []
        assert client.post(f'/jobs/{job_id}/resolve-export', json={'clip_ids': [str(uuid4())]}).status_code == 422
        result = client.post(f'/jobs/{job_id}/resolve-export', json={'clip_ids': [clip_id]})
        assert result.status_code == 200, result.text
        payload = result.json()
        assert client.get(payload['timeline_url']).status_code == 200
        assert client.get(payload['srt_url']).status_code == 200
    folder = Path(payload['folder_path'])
    timeline = otio.adapters.read_from_file(str(folder/'timeline.otio'))
    assert len(timeline.tracks[0]) == 2
    assert timeline.tracks[1].kind == otio.schema.TrackKind.Audio
    assert len(timeline.tracks[1]) == 2
    first, second = timeline.tracks[0]
    assert first.media_reference.target_url == path.resolve().as_uri()
    assert (first.source_range.start_time.value, first.source_range.duration.value) == (20, 22)
    assert (second.source_range.start_time.value, second.source_range.duration.value) == (59, 16)
    assert first.markers[0].name == 'Payoff'
    assert 'comeback' in (folder/'01.srt').read_text()
    assert saved['revision_id'] == json.loads((folder/'manifest.json').read_text())['clips'][0]['revision_id']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_handoff_does_not_create_edit_revision(handoff_source):
    _, job_id, clip_id, _ = handoff_source
    from app.core.recipes import read_edit
    assert read_edit(job_id, clip_id) is None
    with TestClient(app) as client:
        assert client.post(f'/jobs/{job_id}/resolve-export', json={}).status_code == 200
    assert read_edit(job_id, clip_id) is None


def test_mcp_stdio_edit_render_handoff_via_api(handoff_source, monkeypatch):
    import uvicorn
    _, job_id, clip_id, _ = handoff_source
    mcp_python = Path(__file__).resolve().parents[1]/'.mcp-venv/bin/python'
    if not mcp_python.is_file():
        pytest.skip('Install requirements-mcp.txt in the separate .mcp-venv to run the local MCP smoke test')
    monkeypatch.setattr('app.core.queue.get_queue', lambda: MagicMock())
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic()+5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    env = {'PATH': os.environ.get('PATH', ''), 'CLIPOS_MCP_JOB_ID': job_id,
           'CLIPOS_API_URL': f'http://127.0.0.1:{port}'}
    try:
        result = subprocess.run([str(mcp_python), str(Path(__file__).resolve().parents[1]/'tools/smoke_mcp.py'), clip_id],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr[-2500:]
        outcome = json.loads(result.stdout)
        assert outcome['render_id'] and outcome['handoff_id']
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
