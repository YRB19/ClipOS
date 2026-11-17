import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import requests
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.config import settings
from app.core import selection, signals, chunks
from app.core.artifacts import write_json
from app.core.assets import ensure_source
from app.core.media import ffmpeg
from app.core.llm_fallback import call_llm_with_fallback, ProvidersUnavailable
from app.core.selection_validation import candidates_response, scores_response
from app.models.job import Job
from app.models.selection import ModelCall, MomentMarker, SelectionFeedback


def candidate(clip_id='spoken', start=0., end=20., evidence=None, **kwargs):
    return dict(clip_id=clip_id, start=start, end=end, hook_text='Payoff', virality_score=.8,
                reasoning='Complete setup and payoff', evidence=evidence or ['transcript'], **kwargs)


def response(content, **kwargs):
    return dict(choices=[dict(message=dict(content=content))], **kwargs)


def scoring(blocks):
    return json.dumps([dict(clip_id=h['clip_id'], title='Moment', final_hook_text='Payoff', final_score=.8,
                            rank_reasoning='Visible payoff with context') for h in blocks])


@pytest.fixture
def source(tmp_path, monkeypatch):
    from app.models.job import engine
    path = tmp_path/'signals.mkv'
    # Black→white at 20s and a two-second loud audio reaction at25s; same recording
    # used for both signal and transcript fixture comparisons.
    subprocess.run([ffmpeg(), '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'color=black:s=160x90:r=2:d=20', '-f', 'lavfi', '-i', 'color=white:s=160x90:r=2:d=20',
        '-f', 'lavfi', '-i', 'sine=frequency=880:duration=40',
        '-filter_complex', "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume='if(between(t,25,27),1,0.01)':eval=frame[a]",
        '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'pcm_s16le',
        '-metadata:s:a:0', 'title=Microphone', str(path)], check=True, capture_output=True, timeout=30)
    with Session(engine) as session:
        session.add(Job(job_id='job', source_type='local', status='ingested', video_path=str(path), duration_seconds=40)); session.commit()
    raw = Path(settings.storage_raw)/'job'; raw.mkdir(parents=True)
    write_json(raw/'transcript.json', [dict(word='setup payoff', start=1., end=2.)])
    write_json(raw/'candidates.json', [candidate()])
    write_json(raw/'highlights.json', [candidate()])
    return engine, path


def test_validated_fallback_records_usage_and_does_not_log_secrets(source, monkeypatch, caplog):
    engine, _ = source
    secret = 'secret-key-in-response-body'
    first = SimpleNamespace(call=MagicMock(return_value=response('not json '+secret, usage={'cost': .01})))
    second = SimpleNamespace(call=MagicMock(return_value=response(json.dumps([candidate_payload()]), usage={'prompt_tokens': 20, 'completion_tokens': 30, 'cost': .02})))
    with caplog.at_level(logging.WARNING):
        raw, _ = call_llm_with_fallback([], provider_tiers=[(first, 'call', 'bad'), (second, 'call', 'valid')],
            validate=candidates_response, job_id='job', stage='test', prompt_version='fixture', db_engine=engine)
    assert len(candidates_response(raw)) == 1 and secret not in caplog.text
    with Session(engine) as session:
        calls = session.exec(select(ModelCall).order_by(ModelCall.created_at)).all()
        assert [c.status for c in calls] == ['failed', 'completed']
        assert calls[0].error_type == 'JSONDecodeError'
        assert calls[1].prompt_tokens == 20 and calls[1].cost_credits == .02
        assert calls[1].prompt_version == 'fixture' and calls[1].latency_seconds >= 0


def candidate_payload(**kwargs):
    return dict(start=0., end=20., hook_text='Moment', virality_score=.8, reasoning='Payoff', **kwargs)


@pytest.mark.parametrize('field,value', [('start', -1), ('end', 0), ('end', 500), ('virality_score', 1.1), ('virality_score', float('nan')), ('start', True), ('hook_text', ''), ('reasoning', {})])
def test_invalid_semantics_trigger_fallback(source, field, value):
    bad = candidate_payload(); bad[field] = value
    first = SimpleNamespace(call=MagicMock(return_value=response(json.dumps([bad]))))
    second = SimpleNamespace(call=MagicMock(return_value=response(json.dumps([candidate_payload()]))))
    validate = lambda raw: candidates_response(raw, 0, 40, 15, 30)
    raw, _ = call_llm_with_fallback([], provider_tiers=[(first, 'call', 'bad'), (second, 'call', 'good')], validate=validate)
    assert validate(raw)[0]['end'] == 20 and second.call.call_count == 1


def test_transient_retry_backoff_deadline_and_safe_errors(source, monkeypatch):
    module = SimpleNamespace(call=MagicMock(side_effect=[requests.Timeout('sensitive-response'), response('ok')]))
    waits = []
    monkeypatch.setattr(settings, 'llm_backoff_seconds', 2)
    monkeypatch.setattr('app.core.llm_fallback.time.sleep', waits.append)
    assert call_llm_with_fallback([], provider_tiers=[(module, 'call', 'model')])[0] == 'ok'
    assert waits == [2]
    assert module.call.call_args.kwargs['request_timeout'] <= settings.llm_request_timeout_seconds
    module.call.side_effect = requests.Timeout('sensitive-response')
    with pytest.raises(ProvidersUnavailable) as exc:
        call_llm_with_fallback([], provider_tiers=[(module, 'call', 'model')])
    assert exc.value.retryable and 'sensitive-response' not in str(exc.value)
    # The deadline is checked again after a provider returns.
    clock = [0.]
    monkeypatch.setattr('app.core.llm_fallback.time.monotonic', lambda: clock[0])
    def late(**kwargs): clock[0] += 300; return response('ok')
    module.call.side_effect = late
    with pytest.raises(ProvidersUnavailable, match='deadline'):
        call_llm_with_fallback([], provider_tiers=[(module, 'call', 'model')])


def test_clients_remove_key_logging_and_provider_body(source, monkeypatch, capsys):
    from app.core.nvidia_client import nvidia_chat_completion
    from app.core.openrouter_client import openrouter_chat_completion
    monkeypatch.setattr(settings, 'nvidia_api_key', 'secret-start-end')
    monkeypatch.setattr(settings, 'openrouter_api_key', 'secret-start-end')
    result = MagicMock(ok=False, status_code=429, text='secret-start-end echoed body')
    post = MagicMock(return_value=result)
    monkeypatch.setattr('requests.post', post)
    for client in (nvidia_chat_completion, openrouter_chat_completion):
        with pytest.raises(requests.HTTPError) as exc: client('fixture', [], request_timeout=2)
        assert 'secret' not in str(exc.value) and 'echoed' not in str(exc.value)
    assert capsys.readouterr().out == ''
    assert post.call_args.kwargs['timeout'] == 2
    assert 'request_timeout' not in post.call_args.kwargs['json']


def test_real_audio_scene_events_keep_setup_payoff_and_switches(source, monkeypatch):
    engine, path = source
    asset = ensure_source('job'); window = chunks.windows(40)[0]
    assert signals.extract_events(asset, window, 1) == []
    monkeypatch.setattr(settings, 'selection_audio_enabled', True)
    monkeypatch.setattr(settings, 'selection_scene_enabled', True)
    events = signals.extract_events(asset, window, 1)
    assert any(e['kind']=='scene_change' and abs(e['time']-20) <= 1 for e in events)
    assert any(e['kind']=='audio_reaction' and 25 <= e['time'] <= 27 for e in events)
    candidates = signals.event_candidates(asset, window, 1, engine)
    assert all(h['virality_score'] == 0 for h in candidates)
    assert any(h['start'] < 20 and h['end'] > 27 for h in candidates)
    merged = signals.merge_candidates(candidates + [candidate(start=12., end=38.)])
    assert len(merged) == 1 and set(merged[0]['evidence']) == {'transcript', 'scene_change', 'audio_reaction'}
    assert path.is_file()


def test_manual_marker_invalidates_only_affected_selection_chunk(source, monkeypatch):
    engine, _ = source
    monkeypatch.setattr(settings, 'chunk_seconds', 30)
    asset = ensure_source('job')
    for i in range(2): chunks.publish('job', 'transcribe_chunk', str(i), [])
    before = [chunks.context('job', 'highlights_chunk', str(i))[2] for i in range(2)]
    asr_before = chunks.context('job', 'transcribe_chunk', '0')[2]
    with Session(engine) as session:
        session.add(MomentMarker(job_id='job', source_fingerprint=asset.fingerprint, time_seconds=20, label='Boss down')); session.commit()
    assert chunks.context('job', 'highlights_chunk', '0')[2] != before[0]
    assert chunks.context('job', 'highlights_chunk', '1')[2] == before[1]
    assert chunks.context('job', 'transcribe_chunk', '0')[2] == asr_before
    assert any('manual_marker' in h['evidence'] for h in chunks.highlights_chunk('job', '0'))


def test_vision_shortlist_real_frames_validated_response_and_cache(source, monkeypatch):
    engine, _ = source
    monkeypatch.setattr(settings, 'selection_vision_enabled', True)
    monkeypatch.setattr(settings, 'vision_models', ['openrouter:fixture-vision'])
    payload = dict(quality=.9, confidence=.8, has_payoff=True, action='Transition', reaction='unknown',
                   context='Fixture', reason='Visible action', event_key='fixture-event')
    calls = []
    def provider(**kwargs):
        calls.append(kwargs)
        return response(json.dumps(payload))
    monkeypatch.setattr('app.core.openrouter_client.openrouter_chat_completion', provider)
    result = selection.assess_candidate('job', 'spoken')
    assert result['sample_count'] == 6 and result['has_payoff']
    assert selection.assess_candidate('job', 'spoken') == result and len(calls) == 1
    content = calls[0]['messages'][1]['content']
    images = [part for part in content if part['type']=='image_url']
    assert len(images) == 6 and all(p['image_url']['url'].startswith('data:image/jpeg;base64,') for p in images)
    assert calls[0]['messages'][0]['content'] == selection.VISION_PROMPT
    with Session(engine) as session: assert session.exec(select(ModelCall).where(ModelCall.stage=='assess')).one().model == 'fixture-vision'


def test_rank_silent_action_loud_noise_and_replays(source, monkeypatch):
    engine, _ = source
    monkeypatch.setattr(settings, 'selection_vision_enabled', True)
    raw = Path(settings.storage_raw)/'job'
    entries = [candidate('spoken', 0., 10.), candidate('silent', 12., 24., ['scene_change']),
               candidate('noise', 0., 15., ['audio_reaction']), candidate('replay', 25., 37., ['scene_change'])]
    write_json(raw/'candidates.json', entries)
    def visual(job_id, clip_id, db_engine=None):
        return dict(quality=.9, confidence=.9, has_payoff=clip_id!='noise', action='win', reaction='unknown',
                    context='match', reason='payoff' if clip_id!='noise' else 'loud menu', event_key='repeat' if clip_id in ('silent','replay') else clip_id,
                    frame_hashes=['1234567890abcdef','876543210fedcba9','abababab12121212'])
    monkeypatch.setattr(selection, 'assessment', visual)
    llm = MagicMock(side_effect=lambda **kw: (scoring(json.loads(kw['messages'][1]['content'])), 'mock'))
    monkeypatch.setattr(selection, 'call_llm_with_fallback', llm)
    result = selection.rank_candidates('job')
    assert {h['clip_id'] for h in result['selected']} == {'spoken', 'silent'}
    assert next(h for h in result['ranked'] if h['clip_id']=='noise')['final_score'] <= .3
    assert selection.rank_candidates('job') == result and llm.call_count == 1
    # Feedback changes global selection but reuses the same provider batch.
    with Session(engine) as session:
        session.add(SelectionFeedback(key='job:spoken', job_id='job', clip_id='spoken', decision='accepted')); session.commit()
    selection.rank_candidates('job')
    assert llm.call_count == 1
    assert selection.rank_signature('job')


def test_shortlist_retains_nonverbal_candidates_and_rejection(source, monkeypatch):
    engine, _ = source
    monkeypatch.setattr(settings, 'selection_shortlist_count', 4)
    entries = [candidate(str(i), i*2., i*2.+15.) for i in range(10)]
    entries += [candidate('visual', 20., 40., ['scene_change'], evidence_strength=.8)]
    write_json(Path(settings.storage_raw)/'job'/'candidates.json', entries)
    assert 'visual' in [h['clip_id'] for h in selection.shortlist('job')]
    with Session(engine) as session:
        session.add(SelectionFeedback(key='job:visual', job_id='job', clip_id='visual', decision='rejected')); session.commit()
    assert 'visual' not in [h['clip_id'] for h in selection.shortlist('job')]


def test_rank_does_not_render_unassessed_silence_even_if_model_scores_high(source, monkeypatch):
    item = dict(candidate(evidence=['silent_unassessed']), final_score=1.)
    assert selection.choose_ranked([item], {}) == []
    assert len(selection.choose_ranked([item], {'spoken': 'accepted'})) == 1
    assert selection.choose_ranked([item], {'spoken': 'rejected'}) == []


def test_strict_score_ids_reject_missing_duplicate_unknown():
    for entries in ([], [dict(clip_id='wrong')], [dict(clip_id='one'), dict(clip_id='one')]):
        with pytest.raises(ValueError): scores_response(json.dumps(entries), ['one'])


def test_feedback_marker_routes_and_metrics(source):
    from app.main import app
    from app.models.job import get_session
    from app.models.execution import PipelineRun
    engine, _ = source
    def session_override():
        with Session(engine) as session: yield session
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            assert client.post('/jobs/job/markers', json={'time_seconds': 41}).status_code == 422
            marker = client.post('/jobs/job/markers', json={'time_seconds': 20, 'label': 'Boss'}).json()
            assert client.get('/jobs/job/markers').json()[0]['marker_id'] == marker['marker_id']
            assert client.put('/jobs/job/clips/no/feedback', json={'decision': 'accepted'}).status_code == 404
            assert client.put('/jobs/job/clips/spoken/feedback', json={'decision': 'accepted', 'review_seconds': 4}).status_code == 200
            assert client.get('/jobs/job/candidates').json()[0]['feedback'] == 'accepted'
            (Path(settings.storage_raw)/'job'/'candidates.json').unlink()
            assert client.get('/jobs/job/candidates').json()[0]['selected'] is True
            assert client.get('/jobs/job/selection-metrics').json()['feedback']['review_seconds'] == 4
            with Session(engine) as session:
                session.add(PipelineRun(run_id='running', job_id='job', active_key='job')); session.commit()
            assert client.put('/jobs/job/clips/spoken/feedback', json={'decision': 'rejected'}).status_code == 409
            assert client.post('/jobs/job/markers', json={'time_seconds': 10}).status_code == 409
    finally: app.dependency_overrides.clear()


def test_evaluation_counts_missed_repeats_and_unknown_review():
    from app.core.evaluation import compare
    data = dict(dataset_kind='synthetic', recordings=[dict(recording_id='fixture', duration_seconds=100,
        truth=[dict(start=10, end=20), dict(start=40, end=50)],
        transcript_only=dict(predictions=[dict(clip_id='a', start=5, end=25), dict(clip_id='replay', start=5, end=25)]),
        combined=dict(predictions=[dict(clip_id='a', start=5, end=25, accepted=True), dict(clip_id='b', start=35, end=55, accepted=False)], review_seconds=15))])
    result = compare(data)['recordings'][0]
    assert result['transcript_only']['missed_highlights'] == 1
    assert result['transcript_only']['duplicate_candidates'] == 1
    assert result['transcript_only']['acceptance_rate'] is None and result['transcript_only']['review_seconds'] is None
    assert result['combined']['missed_highlights'] == 0 and result['combined']['acceptance_rate'] == .5


def test_replay_detection_requires_specific_context_and_nonuniform_frames():
    hashes = ['1234567890abcdef','876543210fedcba9','abababab12121212']
    a = dict(visual=dict(event_key='specific replay', frame_hashes=hashes))
    assert selection.repeated(a, a)
    assert not selection.repeated(a, dict(visual=dict(event_key='different kill', frame_hashes=hashes)))
    assert not selection.repeated(a, dict(visual=dict(event_key='specific replay', frame_hashes=['ffffffffffffffff']*3)))
    blank = dict(visual=dict(event_key='menu', frame_hashes=['0000000000000000']*3))
    assert not selection.repeated(blank, blank)


def test_failed_rank_batch_resumes_without_repeating_completed_batch(source, monkeypatch):
    raw = Path(settings.storage_raw)/'job'
    entries = [candidate(str(i), float(i*5), float(i*5+15)) for i in range(5)]
    write_json(raw/'candidates.json', entries)
    write_json(raw/'transcript.json', [dict(word='payoff', start=float(i*5+1), end=float(i*5+2)) for i in range(5)])
    calls = []
    def provider(**kwargs):
        blocks = json.loads(kwargs['messages'][1]['content']); calls.append([h['clip_id'] for h in blocks])
        if len(calls) == 2: raise ProvidersUnavailable('fixture failure')
        return scoring(blocks), 'mock'
    monkeypatch.setattr(selection, 'call_llm_with_fallback', provider)
    with pytest.raises(ProvidersUnavailable): selection.rank_candidates('job')
    selection.rank_candidates('job')
    assert len(calls) == 3 and calls[1] == calls[2] and calls[0] != calls[2]


def test_small_rank_budget_remains_bounded_with_dense_text(source, monkeypatch):
    monkeypatch.setattr(settings, 'analysis_max_chars', 2000)
    raw = Path(settings.storage_raw)/'job'
    write_json(raw/'transcript.json', [dict(word='x'*1000, start=1., end=2.) for _ in range(30)])
    def provider(**kwargs):
        assert len(kwargs['messages'][1]['content']) <= 2000
        return scoring(json.loads(kwargs['messages'][1]['content'])), 'mock'
    monkeypatch.setattr(selection, 'call_llm_with_fallback', provider)
    assert selection.rank_candidates('job')['selected']


def test_vision_invalid_schema_falls_back_with_no_cache_poisoning(source, monkeypatch):
    monkeypatch.setattr(settings, 'selection_vision_enabled', True)
    monkeypatch.setattr(settings, 'vision_models', ['openrouter:bad', 'openrouter:good'])
    def provider(**kwargs):
        if kwargs['model'] == 'bad': return response('{"quality": 900}')
        return response(json.dumps(dict(quality=.8, confidence=.9, has_payoff=True, action='play', reaction='unknown',
            context='game', reason='payoff', event_key='event')))
    monkeypatch.setattr('app.core.openrouter_client.openrouter_chat_completion', provider)
    assert selection.assess_candidate('job', 'spoken')['quality'] == .8
    with Session(source[0]) as session:
        calls = session.exec(select(ModelCall).where(ModelCall.stage=='assess').order_by(ModelCall.created_at)).all()
        assert [(c.model, c.status) for c in calls] == [('bad', 'failed'), ('good', 'completed')]
