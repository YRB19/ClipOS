"""Long scheduling mocks inference; MKV timestamp and track tests use real FFmpeg."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.config import settings
from app.core import chunks, pipeline
from app.core.assets import ensure_source
from app.core.media import ffmpeg
from app.models.job import Job
from app.models.source_import import SourceImport
from app.models.execution import PipelineRun


@pytest.fixture
def source(monkeypatch, tmp_path):
    from app.models.job import engine
    path = tmp_path/'library'/'recording.mkv'
    path.parent.mkdir()
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=64x36:r=1:d=65',
                    '-f', 'lavfi', '-i', 'sine=frequency=220:duration=65',
                    '-f', 'lavfi', '-i', 'sine=frequency=880:duration=65',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-metadata:s:a:0', 'title=Game', '-metadata:s:a:1', 'title=Microphone',
                    str(path)], check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(settings, 'chunk_seconds', 30)
    monkeypatch.setattr(settings, 'chunk_overlap_seconds', 5)
    monkeypatch.setattr(settings, 'media_root', str(path.parent))
    with Session(engine) as session:
        session.add(Job(job_id='job', source_type='local', status='ingested', video_path=str(path), duration_seconds=65))
        session.commit()
    return engine, path


def test_mkv_microphone_vad_offsets_overlap_resume_and_corruption(source, monkeypatch):
    engine, path = source
    assert chunks.selected_track(ensure_source('job')) == 2
    from app.core import transcribe
    model = MagicMock(); model.transcribe.return_value = ([], None)
    monkeypatch.setattr(transcribe, '_get_model', lambda: model)
    assert transcribe._call_whisper_asr('fixture.wav') == []
    assert model.transcribe.call_args.kwargs['vad_filter'] is True
    asr = MagicMock(side_effect=[
        [dict(word='boundary', start=29.7, end=30.1), dict(word='payoff', start=30.1, end=30.8)],
        [dict(word='boundary', start=4.7, end=5.1), dict(word='payoff', start=5.1, end=5.8)], []])
    monkeypatch.setattr(transcribe, '_call_whisper_asr', asr)
    before = path.read_bytes()
    for i in range(3): chunks.transcribe_chunk('job', str(i))
    result = chunks.assemble_transcript('job')
    assert [w['word'] for w in result] == ['boundary', 'payoff']
    assert result[0]['start'] == 29.7 and result[1]['end'] == 30.8
    chunks.transcribe_chunk('job', '0')
    assert asr.call_count == 3 and path.read_bytes() == before
    checkpoint = chunks.context('job', 'transcribe_chunk', '2')[3]
    payload = json.loads(checkpoint.read_text()); payload['data'] = [dict(word='corrupt', start=1, end=2)]
    checkpoint.write_text(json.dumps(payload))
    assert chunks.load('job', 'transcribe_chunk', '2') is None
    assert not list(checkpoint.parent.rglob('*.wav'))


def test_silence_is_successful_visual_eligibility_and_uses_no_provider(source, monkeypatch):
    monkeypatch.setattr('app.core.transcribe._call_whisper_asr', lambda _: [])
    chunks.transcribe_chunk('job', '0')
    candidates = chunks.highlights_chunk('job', '0')
    assert len(candidates) == 1 and candidates[0]['evidence'] == 'silent_unassessed'
    assert candidates[0]['virality_score'] == 0
    assert chunks.highlights_chunk('job', '0') == candidates
    from app.core.scoring import score_and_title_clips
    assert score_and_title_clips('job')[0]['final_score'] == 0


def test_prompt_budget_stable_ids_dedupe_and_settings_invalidation(source, monkeypatch):
    words = [dict(word='x'*100, start=i*.01, end=i*.01+.005) for i in range(2500)]
    chunks.publish('job', 'transcribe_chunk', '0', words)
    chunks.publish('job', 'transcribe_chunk', '1', [dict(word='setup', start=25, end=26)])
    monkeypatch.setattr(settings, 'analysis_max_chars', 2000)
    messages = []
    def llm(**kwargs):
        messages.append(kwargs['messages'])
        return json.dumps([dict(start=25, end=45, hook_text='win', virality_score=.9, reasoning='payoff')]), 'fake'
    monkeypatch.setattr('app.core.highlights.call_llm_with_fallback', llm)
    with pytest.raises(ValueError, match='bounds'): chunks.highlights_chunk('job', '0')
    def valid_llm(**kwargs):
        messages.append(kwargs['messages'])
        return json.dumps([dict(start=10, end=30, hook_text='win', virality_score=.8, reasoning='payoff')]), 'fake'
    monkeypatch.setattr('app.core.highlights.call_llm_with_fallback', valid_llm)
    first = chunks.highlights_chunk('job', '0')
    assert len(messages) > 1 and all(len(m[1]['content']) < 2100 for m in messages)
    assert chunks.highlights_chunk('job', '0') == first
    second = dict(first[0], clip_id='second', start=25, end=40, virality_score=.9, analysis_chunk=1)
    first[0].update(start=20, end=35)
    chunks.publish('job', 'highlights_chunk', '0', [first[0]])
    chunks.publish('job', 'highlights_chunk', '1', [second])
    assert len(chunks.assemble_candidates('job')) == 1
    before_asr = chunks.context('job', 'transcribe_chunk', '0')[2]
    before_selection = chunks.context('job', 'highlights_chunk', '0')[2]
    monkeypatch.setattr(settings, 'target_clip_count', 1)
    assert chunks.context('job', 'transcribe_chunk', '0')[2] == before_asr
    assert chunks.context('job', 'highlights_chunk', '0')[2] != before_selection
    assert chunks.load('job', 'transcribe_chunk', '0') == words
    monkeypatch.setattr(settings, 'asr_model', 'small')
    assert chunks.load('job', 'transcribe_chunk', '0') is None


def test_local_root_symlinks_audio_and_remote_enqueue_failure(source, monkeypatch, tmp_path):
    from app.main import app
    from app.models.job import get_session
    engine, path = source
    def session_override():
        with Session(engine) as session: yield session
    app.dependency_overrides[get_session] = session_override
    outside = tmp_path/'outside.mkv'; outside.write_bytes(path.read_bytes())
    (path.parent/'escape.mkv').symlink_to(outside)
    def offline(*args, **kwargs): raise ConnectionError('offline')
    monkeypatch.setattr('app.core.queue.enqueue_stage', offline)
    try:
        with TestClient(app) as client:
            assert client.get('/ingest/library').json()['files'] == ['recording.mkv']
            for name in ('../outside.mkv', 'escape.mkv'):
                assert client.post('/ingest/local', json={'path': name}).status_code == 422
            probe = client.post('/ingest/local/probe', json={'path': 'recording.mkv'}).json()
            assert probe['audio_tracks'][1]['title'] == 'Microphone'
            assert client.post('/ingest/local', json={'path': 'recording.mkv', 'audio_stream_index': 2}).status_code == 200
            assert client.post('/ingest/local', json={'path': 'recording.mkv', 'audio_stream_index': 9}).status_code == 422
            queued = client.post('/ingest/youtube', json={'url': 'https://youtube.com/watch?v=test'})
            assert queued.status_code == 202
            job_id = queued.json()['job_id']
            with Session(engine) as session:
                assert session.get(Job, job_id).status == 'failed'
                run = session.exec(select(PipelineRun).where(PipelineRun.job_id == job_id)).one()
                assert run.current_stage == 'download' and run.active_key is None
            assert client.post('/ingest/youtube', json={'url': 'file:///etc/passwd'}).status_code == 422
    finally: app.dependency_overrides.clear()


def test_failed_download_is_durable_and_timeout_bounded(source, monkeypatch):
    from app.core.imports import download_source
    engine, _ = source
    with Session(engine) as session:
        session.add(SourceImport(job_id='job', kind='remote', url='https://youtu.be/test')); session.commit()
    calls = []
    def fail(command, **kwargs):
        calls.append((command, kwargs)); raise subprocess.TimeoutExpired(command, 30)
    monkeypatch.setattr('app.core.imports.subprocess.run', fail)
    with pytest.raises(subprocess.TimeoutExpired): download_source('job')
    assert calls[0][1]['timeout'] == settings.download_timeout_seconds
    with Session(engine) as session: assert session.get(SourceImport, 'job').status == 'failed'


def test_three_hour_schedule_cancel_resume_and_early_render(source, monkeypatch):
    """36 five-minute sections; does not claim inference latency or clip quality."""
    engine, path = source
    monkeypatch.setattr(settings, 'chunk_seconds', 300)
    asset = ensure_source('job'); metadata = json.loads(asset.metadata_json); metadata['duration'] = 10800
    from app.models.edit import SourceAsset
    with Session(engine) as session:
        job = session.get(Job, 'job'); job.duration_seconds = 10800; session.add(job)
        current = session.get(SourceAsset, 'job'); current.metadata_json = json.dumps(metadata); session.add(current); session.commit()
    pending, stages = [], []
    monkeypatch.setattr('app.core.queue.enqueue_stage', lambda task, stage, delay=0: pending.append(task))
    original_signature, original_input = pipeline.signature, pipeline.input_signature
    def execute(a):
        stages.append((a.stage, a.clip_id))
        if a.stage == 'transcribe_chunk':
            index = int(a.clip_id)
            chunks.publish('job', a.stage, a.clip_id, [dict(word='setup', start=index*300+10, end=index*300+11)])
            chunks.assemble_transcript('job')
        elif a.stage == 'highlights_chunk':
            index = int(a.clip_id)
            chunks.publish('job', a.stage, a.clip_id, [dict(clip_id=f'clip{index}', start=index*300+10,
                end=index*300+30, hook_text='payoff', reasoning='fixture', virality_score=.5, analysis_chunk=index)])
            chunks.assemble_candidates('job')
    monkeypatch.setattr(pipeline, '_execute', execute)
    monkeypatch.setattr(pipeline, 'signature', lambda job, stage, clip=None: original_signature(job, stage, clip) if stage.endswith('_chunk') else stage+str(clip))
    monkeypatch.setattr(pipeline, 'input_signature', lambda job, stage, clip=None: original_input(job, stage, clip) if stage.endswith('_chunk') else stage+str(clip))
    run, _ = pipeline.claim_run('job'); pipeline.dispatch_next(run.run_id)
    for _ in range(5): pipeline.run_stage(pending.pop(0))
    assert stages[:5] == [('assets', None), ('transcribe_chunk', '0'), ('highlights_chunk', '0'), ('recipe', 'clip0'), ('render', 'clip0')]
    with Session(engine) as session:
        current = session.get(PipelineRun, run.run_id); current.cancel_requested = True; session.add(current); session.commit()
    pipeline.run_stage(pending.pop(0))
    resumed, created = pipeline.claim_run('job'); assert created
    pipeline.dispatch_next(resumed.run_id)
    for _ in range(200):
        if not pending: break
        pipeline.run_stage(pending.pop(0))
    else: pytest.fail('Long schedule failed to terminate')
    assert sum(s == 'transcribe_chunk' for s, _ in stages) == 36
    assert sum(s == 'highlights_chunk' for s, _ in stages) == 36
    assert sum(s == 'render' for s, _ in stages) == 10
    with Session(engine) as session: assert session.get(PipelineRun, resumed.run_id).status == 'completed'


def test_cache_cleanup_protects_active_jobs_originals_and_saved_edits(source, monkeypatch):
    from app.core.cache import cache_plan, clean_cache, ensure_cache_capacity
    from app.models.edit import ClipEdit, EditRevision
    engine, original = source
    monkeypatch.setattr(settings, 'cache_max_gb', 0.000001)
    root = Path(settings.storage_raw)
    for job in ('active', 'job'):
        folder = root/job/'chunks'; folder.mkdir(parents=True)
        (folder/'derived').write_bytes(b'x'*2000)
    nested_source = root/'job'/'assets'/'imported.mkv'
    nested_source.parent.mkdir(parents=True)
    nested_source.write_bytes(b'original')
    with Session(engine) as session:
        session.add(Job(job_id='nested', source_type='local', status='ingested',
                        video_path=str(nested_source), duration_seconds=1))
        session.add(PipelineRun(run_id='active-run', job_id='active', active_key='active'))
        session.add(ClipEdit(key='job:clip', job_id='job', clip_id='clip', revision=1, revision_id='saved'))
        session.add(EditRevision(revision_id='saved', edit_key='job:clip', revision=1, recipe_json='{}'))
        session.commit()
    with pytest.raises(ValueError, match='CACHE_MAX_GB'): ensure_cache_capacity()
    plan = cache_plan()
    assert [item['job_id'] for item in plan['remove']] == ['job']
    clean_cache()
    assert not (root/'job'/'chunks').exists()
    assert (root/'active'/'chunks'/'derived').is_file() and original.is_file() and nested_source.is_file()
    with Session(engine) as session: assert session.get(EditRevision, 'saved').recipe_json == '{}'


def test_successful_queued_download_continues_and_reuses_on_resume(source, monkeypatch):
    from app.core.imports import download_source
    engine, original = source
    with Session(engine) as session:
        session.add(SourceImport(job_id='job', kind='remote', url='https://youtu.be/test'))
        session.commit()
    commands = []
    def downloaded(command, **kwargs):
        commands.append(command)
        Path(command[command.index('-o')+1]).write_bytes(original.read_bytes())
    # Patch only yt-dlp; leave ffprobe's subprocess runner intact.
    import app.core.imports as imports
    real = subprocess.run
    monkeypatch.setattr(imports.subprocess, 'run', lambda cmd, **kw: downloaded(cmd, **kw) if cmd[0] == 'yt-dlp' else real(cmd, **kw))
    download_source('job')
    with Session(engine) as session:
        job = session.get(Job, 'job'); request = session.get(SourceImport, 'job')
        assert job.video_path.endswith('.mkv') and job.duration_seconds >= 65 and request.status == 'ready'
    signature = pipeline.signature('job', 'download')
    assert signature and '--no-playlist' in commands[0] and '--' in commands[0]
    assert pipeline._plan('job', [])[0] == ('download', None)


def test_delayed_audio_is_padded_before_asr_to_preserve_source_silence(source, monkeypatch, tmp_path):
    import array
    import wave
    engine, _ = source
    delayed = tmp_path/'delayed.mkv'
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=64x36:r=1:d=8',
                    '-itsoffset', '2', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=6',
                    '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-c:a', 'pcm_s16le', str(delayed)],
                   check=True, capture_output=True, timeout=30)
    with Session(engine) as session:
        session.add(Job(job_id='delayed', source_type='local', status='ingested', video_path=str(delayed), duration_seconds=8))
        session.commit()
    def asr(audio):
        with wave.open(audio) as stream:
            samples = array.array('h', stream.readframes(stream.getnframes()))
            onset = next(i for i, n in enumerate(samples) if abs(n) > 1000)/stream.getframerate()
        assert onset == pytest.approx(2, abs=.05)
        return [dict(word='onset', start=onset, end=onset+.2)]
    monkeypatch.setattr('app.core.transcribe._call_whisper_asr', asr)
    assert chunks.transcribe_chunk('delayed', '0')[0]['start'] == pytest.approx(2, abs=.05)


def test_existing_import_schema_gets_auto_edit_marker_additively(tmp_path):
    import sqlite3
    from sqlmodel import create_engine
    from app.models.job import init_db
    engine = create_engine(f'sqlite:///{tmp_path}/old-phase4.db')
    init_db(engine)
    # Reproduce a version-4 DB predating the automatic caption-refresh marker.
    with sqlite3.connect(tmp_path/'old-phase4.db') as conn:
        conn.execute('ALTER TABLE clip_edits DROP COLUMN generated_signature')
        conn.execute('DELETE FROM schema_migrations WHERE version=5')
        conn.execute("INSERT INTO clip_edits(key,job_id,clip_id,revision,revision_id,updated_at) VALUES ('old:clip','old','clip',1,'rev',CURRENT_TIMESTAMP)")
    init_db(engine); init_db(engine)
    with sqlite3.connect(tmp_path/'old-phase4.db') as conn:
        assert conn.execute("SELECT revision_id,generated_signature FROM clip_edits WHERE key='old:clip'").fetchone() == ('rev', None)
