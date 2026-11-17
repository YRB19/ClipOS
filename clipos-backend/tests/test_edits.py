import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, select
from app.core.assets import ensure_source, prepare_assets, assets_valid
from app.core.media import ffmpeg
from app.core.recipes import EditRecipe, Layout, Region, CropKeyframe, Interval, CaptionWord, AudioTrack, ensure_edit, save_edit, read_edit, RevisionConflict
from app.core.renderer import render_recipe, build_command
from app.core.renders import request_render, run_render, final_output, output_available, public_render
from app.models.job import Job
from app.models.edit import EditRevision, RenderOutput
from app.main import app


@pytest.fixture
def source(tmp_path):
    from app.models.job import engine
    raw = tmp_path/'storage'/'raw'/'job'
    raw.mkdir(parents=True)
    path = raw/'source.mp4'
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30000/1001:d=4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=4',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-metadata:s:a:0', 'title=Game', '-metadata:s:a:1', 'title=Microphone',
                    '-metadata:s:a:0', 'handler_name=Game', '-metadata:s:a:1', 'handler_name=Microphone', str(path)],
                   check=True, capture_output=True, timeout=30)
    (raw/'transcript.json').write_text(json.dumps([dict(word='Hello {test} \\N', start=0.8, end=1.1)]))
    (raw/'highlights.json').write_text(json.dumps([dict(clip_id='clip', start=0.7, end=1.3,
                                                       hook_text='Test', virality_score=0.7, reasoning='fixture')]))
    with Session(engine) as session:
        session.add(Job(job_id='job', source_type='upload', status='done', video_path=str(path), duration_seconds=4))
        session.commit()
    return engine, path


def test_source_probe_fingerprint_assets_reused_and_repaired(source):
    _, path = source
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    asset = ensure_source('job')
    assert asset.fingerprint == before
    meta = json.loads(asset.metadata_json)
    assert meta['frame_rate'] == '30000/1001' and len(meta['audio_tracks']) == 2
    assert 'color_transfer' in meta['color']
    first = prepare_assets('job')
    assert assets_valid(ensure_source('job'))
    with TestClient(app) as client:
        assets = client.get('/jobs/job/assets').json()
        assert assets['status'] == 'ready'
        assert client.get(assets['artifacts']['thumbnail.jpg']).status_code == 200
    assert prepare_assets('job') == first
    Path(first['files']['proxy.mp4']['path']).write_bytes(b'corrupt proxy')
    second = prepare_assets('job')
    assert second['files']['proxy.mp4']['signature'] != first['files']['proxy.mp4']['signature']
    waveform = json.loads(Path(second['files']['waveform.json']['path']).read_text())
    assert waveform['peaks'] and max(waveform['peaks']) > 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_revision_roundtrip_conflict_and_immutable_history(source):
    engine, _ = source
    first = ensure_edit('job', 'clip')
    recipe = EditRecipe.model_validate(first['recipe'])
    assert recipe.captions.words[0].start == pytest.approx(0.1)
    recipe.captions.words[0].word = 'Corrected'
    saved = save_edit('job', 'clip', recipe, first['revision'])
    assert read_edit('job', 'clip') == saved
    with pytest.raises(RevisionConflict):
        save_edit('job', 'clip', recipe, first['revision'])
    with Session(engine) as session:
        assert 'Corrected' not in session.get(EditRevision, first['revision_id']).recipe_json
    assert ensure_edit('job', 'clip') == saved
    def write(_):
        try:
            save_edit('job', 'clip', recipe, saved['revision'])
            return True
        except RevisionConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(write, range(2))) == 1


@pytest.mark.parametrize('change', ['bounds', 'nan', 'version', 'track', 'source', 'duration'])
def test_invalid_recipes_rejected(source, change):
    initial = ensure_edit('job', 'clip')
    data = initial['recipe']
    if change == 'bounds': data['layout']['gameplay']['x'] = 0.5
    if change == 'nan': data['intervals'][0]['start'] = float('nan')
    if change == 'version': data['schema_version'] = 2
    if change == 'track': data['audio']['tracks'][0]['stream_index'] = 99
    if change == 'source': data['source']['job_id'] = 'another-job'
    if change == 'duration': data['intervals'][0]['end'] = 20
    with pytest.raises((ValueError, ValidationError)):
        save_edit('job', 'clip', EditRecipe.model_validate(data), initial['revision'])


@pytest.mark.parametrize('mode', ['fit_background', 'gameplay_facecam', 'manual_crop'])
def test_real_final_layouts_render_from_original_without_intermediates(source, tmp_path, mode):
    _, path = source
    original = path.read_bytes()
    asset = ensure_source('job')
    recipe = EditRecipe.model_validate(ensure_edit('job', 'clip')['recipe'])
    if mode == 'gameplay_facecam':
        recipe.layout = Layout(mode=mode, facecam=Region(x=0.6, y=0.0, width=0.4, height=0.4))
    elif mode == 'manual_crop':
        recipe.layout = Layout(mode=mode, gameplay=Region(x=0.1, width=0.4, height=1),
                               crop_keyframes=[CropKeyframe(time=0, x=0.1, y=0), CropKeyframe(time=0.5, x=0.4, y=0)])
    target = tmp_path/mode/'final.mp4'
    metadata = render_recipe(recipe, asset.source_path, json.loads(asset.metadata_json), 'final', target)
    assert (metadata['width'], metadata['height']) == (1080, 1920)
    assert metadata['audio_tracks'] and metadata['frame_rate'] == '30000/1001'
    assert target.is_file() and path.read_bytes() == original
    assert not (target.parent/'clip.mp4').exists()
    assert '｛test｝ ＼N' in target.with_suffix('.ass').read_text()
    command = build_command(recipe, asset.source_path, json.loads(asset.metadata_json), 'final', target)
    assert command.count('-c:v') == 1
    assert command[command.index('-i')+1] == str(path)
    assert command[command.index('-ss')+1] == '0.7'


def test_multi_interval_preview_audio_mix_and_local_caption_timing(source, tmp_path):
    _, path = source
    asset = ensure_source('job')
    recipe = EditRecipe.model_validate(ensure_edit('job', 'clip')['recipe'])
    recipe.intervals = [Interval(start=0.2, end=0.6), Interval(start=2.1, end=2.6)]
    recipe.captions.words = [CaptionWord(word='First', start=0.1, end=0.3), CaptionWord(word='Second', start=0.5, end=0.8)]
    recipe.audio.tracks = [AudioTrack(stream_index=t['stream_index'], gain=0.5)
                           for t in json.loads(asset.metadata_json)['audio_tracks']]
    output = tmp_path/'preview.mp4'
    meta = render_recipe(recipe, asset.source_path, json.loads(asset.metadata_json), 'preview', output)
    assert (meta['width'], meta['height']) == (360, 640)
    assert abs(meta['duration']-0.9) < 0.15
    assert '0:00:00.50,0:00:00.80' in output.with_suffix('.ass').read_text()


def test_queue_pins_revision_deduplicates_and_preserves_old_outputs(source, monkeypatch):
    engine, _ = source
    first = ensure_edit('job', 'clip')
    queue = MagicMock()
    monkeypatch.setattr('app.core.queue.get_queue', lambda: queue)
    render_id = request_render('job', 'clip', first['revision_id'], 'preview')
    assert request_render('job', 'clip', first['revision_id'], 'preview') == render_id
    assert queue.enqueue.call_count == 1
    recipe = EditRecipe.model_validate(first['recipe'])
    recipe.captions.words[0].word = 'New caption'
    save_edit('job', 'clip', recipe, first['revision'])
    run_render(render_id)
    with Session(engine) as session:
        old = session.get(RenderOutput, render_id)
        assert output_available(old)
        assert 'New caption' not in Path(old.output_path).with_suffix('.ass').read_text()
        assert not public_render(old, read_edit('job', 'clip')['revision_id'])['is_current']
        run_render(render_id)
    assert final_output('job', 'clip') is None


def test_render_failure_is_durable_and_retryable(source, monkeypatch):
    engine, _ = source
    edit = ensure_edit('job', 'clip')
    monkeypatch.setattr('app.core.queue.get_queue', lambda: MagicMock())
    render_id = request_render('job', 'clip', edit['revision_id'], 'final')
    def fail(*args, **kwargs):
        raise RuntimeError('Injected encode failure')
    monkeypatch.setattr('app.core.renders.render_recipe', fail)
    with pytest.raises(RuntimeError, match='Injected encode failure'):
        run_render(render_id)
    with Session(engine) as session:
        failed = session.get(RenderOutput, render_id)
        assert failed.status == 'failed' and failed.active_key is None and failed.output_path is None
    assert request_render('job', 'clip', edit['revision_id'], 'final') != render_id


def test_edit_and_render_api_revision_checks_and_refresh(source, monkeypatch):
    monkeypatch.setattr('app.core.queue.get_queue', lambda: MagicMock())
    with TestClient(app) as client:
        initial = client.get('/jobs/job/clips/clip/edit').json()
        saved = client.put('/jobs/job/clips/clip/edit', json=dict(expected_revision=initial['revision'], recipe=initial['recipe']))
        assert saved.status_code == 200
        assert client.put('/jobs/job/clips/clip/edit', json=dict(expected_revision=initial['revision'], recipe=initial['recipe'])).status_code == 409
        request = client.post('/jobs/job/clips/clip/renders', json=dict(revision_id=saved.json()['revision_id'], kind='final'))
        assert request.status_code == 202
        run_render(request.json()['render_id'])
        outputs = client.get('/jobs/job/clips/clip/renders').json()
        assert outputs[0]['available'] and outputs[0]['is_current'] and outputs[0]['url'].endswith('/final.mp4')
        assert client.get('/jobs/job/highlights').json()[0]['final_render']['url'] == outputs[0]['url']
        download = client.get(outputs[0]['url'], headers={'Range': 'bytes=0-99'})
        assert download.status_code == 206 and len(download.content) == 100
        assert download.headers['content-type'] == 'video/mp4'


def test_automatic_source_pipeline_and_edit_resume_skip_asr_and_selection(source, monkeypatch):
    from app.core import pipeline
    # Legacy queued-stage compatibility; chunk scheduling has dedicated coverage.
    monkeypatch.setattr(pipeline, 'INITIAL_STAGES', ('assets', 'transcribe', 'highlights'))
    from app.models.execution import PipelineRun
    engine, path = source
    pending, stages = [], []
    monkeypatch.setattr('app.core.queue.enqueue_stage', lambda task, stage, delay=0: (pending.append(task), stages.append(stage)))
    transcribe, highlights, score = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr('app.core.transcribe.transcribe_video', transcribe)
    monkeypatch.setattr('app.core.highlights.detect_highlights', highlights)
    monkeypatch.setattr('app.core.scoring.score_and_title_clips', score)
    def execute():
        run, _ = pipeline.claim_run('job')
        pipeline.dispatch_next(run.run_id)
        for _ in range(30):
            if not pending:
                break
            pipeline.run_stage(pending.pop(0))
        else:
            pytest.fail('Pipeline did not finish')
        with Session(engine) as session:
            assert session.get(PipelineRun, run.run_id).status == 'completed'
    execute()
    assert stages == ['assets', 'transcribe', 'highlights', 'recipe', 'render', 'score']
    assert final_output('job', 'clip') is not None
    assert not (path.parents[2]/'clips'/'job'/'clip'/'clip.mp4').exists()
    original = read_edit('job', 'clip')
    for change in ('captions', 'crop'):
        current = read_edit('job', 'clip')
        recipe = EditRecipe.model_validate(current['recipe'])
        if change == 'captions':
            recipe.captions.words[0].word = 'Saved caption correction'
        else:
            recipe.layout = Layout(mode='manual_crop', gameplay=Region(x=0.1, width=0.5, height=1))
        save_edit('job', 'clip', recipe, current['revision'])
        execute()
        assert final_output('job', 'clip').revision_id == read_edit('job', 'clip')['revision_id']
    assert transcribe.call_count == highlights.call_count == score.call_count == 1
    assert read_edit('job', 'clip')['recipe']['captions']['words'][0]['word'] == 'Saved caption correction'
    assert original['revision_id'] != read_edit('job', 'clip')['revision_id']


def test_enqueue_ambiguity_and_lost_render_recover(source, monkeypatch):
    from datetime import timedelta
    from app.core.renders import reconcile_renders
    from app.models.execution import now
    engine, _ = source
    edit = ensure_edit('job', 'clip')
    queue = MagicMock()
    queue.enqueue.side_effect = ConnectionError('Acknowledgement lost')
    monkeypatch.setattr('app.core.queue.get_queue', lambda: queue)
    with pytest.raises(ConnectionError):
        request_render('job', 'clip', edit['revision_id'], 'preview')
    with Session(engine) as session:
        output = session.exec(select(RenderOutput)).one()
        render_id = output.render_id
    run_render(render_id)
    with Session(engine) as session:
        assert session.get(RenderOutput, render_id).status == 'failed'
    queue.enqueue.side_effect = None
    retry = request_render('job', 'clip', edit['revision_id'], 'preview')
    with Session(engine) as session:
        output = session.get(RenderOutput, retry)
        output.heartbeat_at = now()-timedelta(seconds=120)
        session.add(output)
        session.commit()
    monkeypatch.setattr('app.core.execution.rq_state', lambda _: 'missing')
    reconcile_renders()
    with Session(engine) as session:
        assert session.get(RenderOutput, retry).status == 'failed'
    assert request_render('job', 'clip', edit['revision_id'], 'preview') not in (render_id, retry)


def test_visible_trim_order_and_caption_timing_with_real_frames(source, tmp_path):
    from app.core.ffprobe import get_video_metadata
    _, path = source
    # Replace only this isolated fixture with known temporal colors.
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=320x180:r=30:d=2',
                    '-f', 'lavfi', '-i', 'color=blue:s=320x180:r=30:d=2',
                    '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
                    '-c:v', 'libx264', '-preset', 'ultrafast', str(path)], check=True, capture_output=True, timeout=30)
    asset = ensure_source('job')
    recipe = EditRecipe.model_validate(ensure_edit('job', 'clip')['recipe'])
    recipe.intervals = [Interval(start=2.2, end=2.6), Interval(start=0.2, end=0.6)]
    recipe.captions.words = [CaptionWord(word='VISIBLE', start=0.15, end=0.35)]
    output = tmp_path/'timing-preview.mp4'
    metadata = render_recipe(recipe, asset.source_path, json.loads(asset.metadata_json), 'preview', output)
    assert metadata['audio_tracks']  # Silent sources receive an explicit silent AAC track.
    def pixels(time, crop, fmt):
        return subprocess.run([ffmpeg(), '-v', 'error', '-ss', str(time), '-i', str(output),
                               '-vf', crop, '-frames:v', '1', '-pix_fmt', fmt, '-f', 'rawvideo', 'pipe:1'],
                              check=True, capture_output=True, timeout=30).stdout
    blue = pixels(0.05, 'crop=32:32:164:304,scale=1:1', 'rgb24')
    red = pixels(0.55, 'crop=32:32:164:304,scale=1:1', 'rgb24')
    assert blue[2] > 200 and blue[0] < 50
    assert red[0] > 200 and red[2] < 50
    before = sum(value > 170 for value in pixels(0.05, 'crop=360:120:0:500', 'gray'))
    during = sum(value > 170 for value in pixels(0.25, 'crop=360:120:0:500', 'gray'))
    after = sum(value > 170 for value in pixels(0.55, 'crop=360:120:0:500', 'gray'))
    assert during > before+50 and during > after+50


def test_real_rq_preview_task_completes(source, monkeypatch):
    import shutil
    import tempfile
    import time
    from redis import Redis
    from rq import Queue, SimpleWorker
    binary = shutil.which('redis-server')
    if not binary:
        pytest.skip('redis-server is required for the private queue integration check')
    engine, _ = source
    edit = ensure_edit('job', 'clip')
    with tempfile.TemporaryDirectory(prefix='clipos-render-rq-', dir='/private/tmp') as directory:
        socket = str(Path(directory)/'redis.sock')
        server = subprocess.Popen([binary, '--port', '0', '--save', '', '--appendonly', 'no',
                                   '--unixsocket', socket, '--dir', directory], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            connection = Redis(unix_socket_path=socket)
            for _ in range(100):
                try:
                    if connection.ping(): break
                except Exception:
                    time.sleep(0.02)
            else:
                pytest.fail('Private Redis did not start')
            queue = Queue(connection=connection)
            monkeypatch.setattr('app.core.queue.get_queue', lambda: queue)
            render_id = request_render('job', 'clip', edit['revision_id'], 'preview')
            SimpleWorker([queue], connection=connection).work(burst=True)
            with Session(engine) as session:
                output = session.get(RenderOutput, render_id)
                assert output_available(output)
            assert queue.finished_job_registry.get_job_ids() == [render_id]
        finally:
            server.terminate()
            server.wait(timeout=5)


def test_delayed_audio_keeps_source_timing(source, tmp_path):
    import array
    _, path = source
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=320x180:r=30:d=2',
                    '-itsoffset', '0.5', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.5',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(path)],
                   check=True, capture_output=True, timeout=30)
    asset = ensure_source('job')
    recipe = EditRecipe.model_validate(ensure_edit('job', 'clip')['recipe'])
    recipe.intervals = [Interval(start=0, end=1.3)]
    recipe.captions.enabled, recipe.captions.words = False, []
    output = tmp_path/'delayed-preview.mp4'
    render_recipe(recipe, asset.source_path, json.loads(asset.metadata_json), 'preview', output)
    pcm = subprocess.run([ffmpeg(), '-v', 'error', '-i', str(output), '-vn', '-ac', '1', '-ar', '48000',
                          '-f', 's16le', 'pipe:1'], check=True, capture_output=True, timeout=30).stdout
    samples = array.array('h', pcm)
    early = sum(abs(n) for n in samples[2400:12000])/9600
    late = sum(abs(n) for n in samples[31200:40800])/9600
    assert early < 30 and late > 500


def test_automatic_recipe_refreshes_only_affected_words_and_preserves_human_edit(source):
    from app.config import settings
    engine, _ = source
    first = ensure_edit('job', 'clip')
    assert first['recipe']['audio']['tracks'] == [dict(stream_index=1, gain=.5), dict(stream_index=2, gain=1)]
    transcript = Path(settings.storage_raw)/'job'/'transcript.json'
    words = json.loads(transcript.read_text())
    words.append(dict(word='outside', start=3, end=3.2))
    transcript.write_text(json.dumps(words))
    assert ensure_edit('job', 'clip')['revision_id'] == first['revision_id']
    words.append(dict(word='new boundary caption', start=.9, end=1.0))
    transcript.write_text(json.dumps(words))
    refreshed = ensure_edit('job', 'clip')
    assert refreshed['revision'] == first['revision']+1
    recipe = EditRecipe.model_validate(refreshed['recipe'])
    recipe.captions.words[0].word = 'Human correction'
    saved = save_edit('job', 'clip', recipe, refreshed['revision'])
    words.append(dict(word='later ASR', start=1.1, end=1.2))
    transcript.write_text(json.dumps(words))
    assert ensure_edit('job', 'clip') == saved


def test_chunk_pipeline_real_source_render_and_caption_only_resume(source, monkeypatch):
    from app.core import pipeline
    from app.config import settings
    from app.models.execution import PipelineRun
    engine, path = source
    monkeypatch.setattr(settings, 'clip_min_seconds', 1)
    pending = []
    monkeypatch.setattr('app.core.queue.enqueue_stage', lambda task, stage, delay=0: pending.append(task))
    asr = MagicMock(return_value=[dict(word='Gaming payoff', start=.8, end=1.1)])
    selection = MagicMock(return_value=(json.dumps([dict(start=.5, end=2.0, hook_text='Payoff', virality_score=.8, reasoning='fixture')]), 'mock'))
    monkeypatch.setattr('app.core.transcribe._call_whisper_asr', asr)
    monkeypatch.setattr('app.core.highlights.call_llm_with_fallback', selection)
    monkeypatch.setattr('app.core.scoring.score_and_title_clips', lambda _: None)
    def rank_llm(**kwargs):
        block = json.loads(kwargs['messages'][1]['content'])
        return json.dumps([dict(clip_id=h['clip_id'], title='Payoff', final_hook_text='Payoff',
                               final_score=.8, rank_reasoning='Setup and payoff') for h in block]), 'mock'
    monkeypatch.setattr('app.core.selection.call_llm_with_fallback', rank_llm)
    def execute():
        run, _ = pipeline.claim_run('job'); pipeline.dispatch_next(run.run_id)
        for _ in range(20):
            if not pending: break
            pipeline.run_stage(pending.pop(0))
        else: pytest.fail('Chunk pipeline did not terminate')
        with Session(engine) as session: assert session.get(PipelineRun, run.run_id).status == 'completed'
    execute()
    candidates = json.loads((path.parent/'highlights.json').read_text())
    clip_id = candidates[0]['clip_id']
    assert final_output('job', clip_id) is not None
    current = read_edit('job', clip_id)
    recipe = EditRecipe.model_validate(current['recipe']); recipe.captions.words[0].word = 'Corrected caption'
    saved = save_edit('job', clip_id, recipe, current['revision'])
    execute()
    assert final_output('job', clip_id).revision_id == saved['revision_id']
    assert asr.call_count == selection.call_count == 1
    (path.parent/'transcript.json').unlink()
    (path.parent/'highlights.json').write_text('{corrupt')
    execute()
    assert asr.call_count == selection.call_count == 1
    assert read_edit('job', clip_id)['revision_id'] == saved['revision_id']
