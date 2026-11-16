"""Reliability contracts: isolated DB/media, no production Redis or model calls."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from app.config import settings
from app.core import pipeline as p
from app.core import execution as execution
from app.main import app
from app.models.job import Job, init_db, get_session
from app.models.execution import PipelineRun, StageAttempt, ClipState, now


@pytest.fixture
def system(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/library.db', connect_args={'check_same_thread': False})
    init_db(engine)
    monkeypatch.setattr(p, 'engine', engine)
    monkeypatch.setattr('app.main.init_db', lambda: init_db(engine))
    monkeypatch.setattr(execution, 'engine', engine)
    monkeypatch.setattr(settings, 'storage_raw', str(tmp_path / 'raw'))
    monkeypatch.setattr(settings, 'storage_clips', str(tmp_path / 'clips'))
    raw = Path(settings.storage_raw) / 'job'
    raw.mkdir(parents=True)
    (raw / 'source.mp4').write_bytes(b'immutable fixture source')
    with Session(engine) as session:
        session.add(Job(job_id='job', source_type='upload', status='ingested',
                        video_path=str(raw / 'source.mp4'), duration_seconds=10))
        session.commit()
    pending, calls = [], []
    monkeypatch.setattr('app.core.queue.enqueue_stage', lambda task_id, stage, delay=0: pending.append(task_id))
    monkeypatch.setattr(execution, 'rq_state', lambda _: 'queued')
    monkeypatch.setattr(p, '_media_signature', lambda path, size, mtime, stage: f'{size}:{mtime}')

    def execute(attempt):
        calls.append(attempt.unit_key)
        path = p.output_path(attempt.job_id, attempt.stage, attempt.clip_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if attempt.stage == 'transcribe':
            data = [dict(word='Hello', start=0.1, end=0.5)]
            path.write_text(json.dumps(data))
        elif attempt.stage == 'highlights':
            path.write_text(json.dumps([dict(clip_id=c, start=0.0, end=1.0, hook_text=c,
                                             virality_score=0.5, reasoning='fixture') for c in ('bad', 'good')]))
        elif attempt.stage == 'score':
            data = json.loads(path.read_text())
            for h in data:
                h['title'] = 'A title'
            path.write_text(json.dumps(data))
        elif attempt.stage == 'captions':
            path.write_text('[Events]\n')
        else:
            path.write_bytes(b'fixture output')
    monkeypatch.setattr(p, '_execute', execute)
    return engine, pending, calls, execute


def drain(pending):
    errors = []
    for _ in range(100):
        if not pending:
            return errors
        try:
            p.run_stage(pending.pop(0))
        except Exception as exc:
            errors.append(exc)
    pytest.fail('Dispatch loop did not finish')


def latest(engine):
    with Session(engine) as session:
        return execution.latest_run(session, 'job')


def test_two_concurrent_starts_have_one_active_run(system):
    engine, pending, _, _ = system
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: p.claim_run('job'), range(2)))
    assert runs[0][0].run_id == runs[1][0].run_id
    assert sum(created for _, created in runs) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: p.dispatch_next(runs[0][0].run_id), range(2)))
    assert len(pending) == 1


def test_completed_tasks_and_duplicate_delivery(system):
    engine, pending, calls, _ = system
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    first = pending[0]
    assert drain(pending) == []
    p.run_stage(first)
    assert calls.count('transcribe') == 1
    assert latest(engine).status == 'completed'
    with Session(engine) as session:
        status = execution.snapshot(session, session.get(Job, 'job'))
        assert status['completed_units'] == status['total_units'] == 11
        assert status['completed_clips'] == 2
        assert all(a['elapsed_seconds'] is not None for a in status['stages'])


def test_clip_failure_propagates_and_other_clip_completes_then_resume(system, monkeypatch):
    engine, pending, calls, execute = system
    def fail_one(a):
        if a.unit_key == 'cut:bad':
            calls.append(a.unit_key)
            raise RuntimeError('Injected cut failure')
        execute(a)
    monkeypatch.setattr(p, '_execute', fail_one)
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    errors = drain(pending)
    assert len(errors) == 1 and 'Injected cut failure' in str(errors[0])
    assert 'reframe:bad' not in calls
    assert 'burn:good' in calls
    assert latest(engine).status == 'completed_with_errors'
    with Session(engine) as session:
        assert session.get(Job, 'job').status == 'failed'
        assert session.get(ClipState, 'job:good').status == 'completed'
    monkeypatch.setattr(p, '_execute', execute)
    retry, _ = p.claim_run('job')
    p.dispatch_next(retry.run_id)
    assert drain(pending) == []
    assert calls.count('transcribe') == 1
    assert calls.count('highlights') == 1
    assert calls.count('burn:good') == 1
    assert latest(engine).status == 'completed'


def test_invalid_checkpoint_recomputes_only_dependent_work(system):
    engine, pending, calls, _ = system
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    drain(pending)
    p.output_path('job', 'captions', 'good').write_text('corrupt')
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    drain(pending)
    assert calls.count('captions:good') == 2
    assert calls.count('burn:good') == 1  # restored ASS content is identical
    assert calls.count('transcribe') == 1
    assert calls.count('cut:good') == 1


def test_transient_retries_bounded_and_permanent_errors_not_retried(system, monkeypatch):
    engine, pending, _, _ = system
    monkeypatch.setattr(p, '_execute', lambda _: (_ for _ in ()).throw(requests.Timeout('temporary')))
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    assert len(drain(pending)) == 3
    assert latest(engine).status == 'failed'
    with Session(engine) as session:
        attempts = list(session.exec(select(StageAttempt)).all())
        assert len(attempts) == 3
        assert all(a.status == 'failed' for a in attempts)


def test_enqueue_failure_releases_claim_and_fences_ambiguous_delivery(system, monkeypatch):
    engine, pending, calls, _ = system
    def ambiguous(task_id, stage, delay=0):
        pending.append(task_id)
        raise ConnectionError('Lost acknowledgement')
    monkeypatch.setattr('app.core.queue.enqueue_stage', ambiguous)
    run, _ = p.claim_run('job')
    with pytest.raises(ConnectionError):
        p.dispatch_next(run.run_id)
    assert latest(engine).active_key is None
    drain(pending)
    assert calls == []
    new, created = p.claim_run('job')
    assert created and new.run_id != run.run_id


def test_killed_worker_reconciles_then_resumes_checkpoint(system, monkeypatch):
    engine, pending, calls, _ = system
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    p.run_stage(pending.pop(0))
    with Session(engine) as session:
        a = session.get(StageAttempt, pending.pop(0))
        a.status, a.started_at = 'running', now() - timedelta(seconds=10)
        session.add(a)
        session.commit()
    monkeypatch.setattr(execution, 'rq_state', lambda _: 'failed')
    execution.reconcile(run.run_id)
    assert latest(engine).status == 'failed'
    retry, _ = p.claim_run('job')
    p.dispatch_next(retry.run_id)
    assert drain(pending) == []
    assert calls.count('transcribe') == 1


def test_dispatch_crash_gap_is_reconciled(system):
    engine, pending, _, _ = system
    run, _ = p.claim_run('job')
    execution.reconcile(run.run_id)
    assert len(pending) == 1


def test_queued_cancellation_fences_task_and_resume(system):
    engine, pending, calls, _ = system
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    with Session(engine) as session:
        r = session.get(PipelineRun, run.run_id)
        r.cancel_requested = True
        session.add(r)
        session.commit()
    drain(pending)
    assert latest(engine).status == 'cancelled'
    assert calls == []
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    assert drain(pending) == []
    assert latest(engine).status == 'completed'


def test_running_cancellation_checkpoints_current_step(system, monkeypatch):
    engine, pending, calls, execute = system
    def cancel(a):
        execute(a)
        with Session(engine) as session:
            r = session.get(PipelineRun, a.run_id)
            r.cancel_requested = True
            session.add(r)
            session.commit()
    monkeypatch.setattr(p, '_execute', cancel)
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    drain(pending)
    assert calls == ['transcribe']
    assert latest(engine).status == 'cancelled'
    monkeypatch.setattr(p, '_execute', execute)
    run, _ = p.claim_run('job')
    p.dispatch_next(run.run_id)
    drain(pending)
    assert calls.count('transcribe') == 1


def test_api_duplicate_start_progress_cancel_resume_and_legacy_job(system):
    engine, pending, _, _ = system
    def sessions():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    try:
        # No app lifespan: all DB state is this fixture, never the user's library.
        with TestClient(app, raise_server_exceptions=True) as client:
            a = client.post('/jobs/job/run')
            b = client.post('/jobs/job/run')
            assert a.status_code == b.status_code == 200
            assert a.json()['run_id'] == b.json()['run_id']
            assert len(pending) == 1
            assert client.post('/jobs/job/transcribe').status_code == 409
            progress = client.get('/jobs/job/queue_status').json()
            assert progress['execution_stage'] == 'transcribe'
            assert client.post('/jobs/job/cancel').json()['status'] == 'cancelled'
            drain(pending)
            assert client.post('/jobs/job/resume').status_code == 200
            drain(pending)
            detail = client.get('/jobs/job').json()
            assert detail['status'] == 'done'
            clips = client.get('/jobs/job/highlights').json()
            assert clips[0]['available_variants'] == ['clip.mp4', 'clip_vertical.mp4', 'clip_captioned.mp4']
            assert client.get('/jobs/missing/queue_status').status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_migrations_on_backed_up_legacy_db_are_repeatable(tmp_path):
    original, migrated = tmp_path / 'legacy.db', tmp_path / 'migration-copy.db'
    with sqlite3.connect(original) as conn:
        conn.execute('CREATE TABLE jobs(job_id TEXT PRIMARY KEY, source_type TEXT, status TEXT, '
                     'created_at TIMESTAMP, video_path TEXT, duration_seconds FLOAT)')
        conn.execute("INSERT INTO jobs VALUES ('old','upload','ingested',CURRENT_TIMESTAMP,'original.mp4',123)")
        conn.commit()
        with sqlite3.connect(migrated) as backup:
            conn.backup(backup)
    engine = create_engine(f'sqlite:///{migrated}')
    init_db(engine)
    init_db(engine)
    with Session(engine) as session:
        assert session.get(Job, 'old').duration_seconds == 123
        assert session.get(Job, 'old').updated_at is not None
    with sqlite3.connect(migrated) as conn:
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 2
    with sqlite3.connect(original) as conn:
        assert 'updated_at' not in [row[1] for row in conn.execute('PRAGMA table_info(jobs)')]


def test_real_rq_worker_records_failure_and_completes_peer(system, monkeypatch):
    """Use a private socket Redis, never the user's server or queues."""
    import shutil
    import subprocess
    import tempfile
    import time
    from redis import Redis
    from rq import Queue, SimpleWorker
    server_binary = shutil.which('redis-server')
    if not server_binary:
        pytest.skip('redis-server is required for the isolated RQ integration check')
    engine, _, calls, execute = system
    with tempfile.TemporaryDirectory(prefix='clipos-rq-', dir='/private/tmp') as directory:
        socket = str(Path(directory) / 'redis.sock')
        server = subprocess.Popen([server_binary, '--port', '0', '--save', '', '--appendonly', 'no',
                                   '--unixsocket', socket, '--unixsocketperm', '700', '--dir', directory],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            connection = Redis(unix_socket_path=socket)
            for _ in range(100):
                try:
                    if connection.ping():
                        break
                except Exception:
                    if server.poll() is not None:
                        pytest.fail(f'Private Redis failed: {server.stderr.read().decode()}')
                    time.sleep(0.02)
            else:
                pytest.fail('Private Redis did not start')
            queue = Queue(connection=connection)
            monkeypatch.setattr('app.core.queue.get_queue', lambda: queue)
            # Undo the transport stub installed by system, retaining private Redis.
            from app.core import queue as transport
            from importlib import reload
            reload(transport)
            monkeypatch.setattr(transport, 'get_queue', lambda: queue)
            def fail(a):
                if a.unit_key == 'cut:bad':
                    raise RuntimeError('Injected queue failure')
                execute(a)
            monkeypatch.setattr(p, '_execute', fail)
            run, _ = p.claim_run('job')
            p.dispatch_next(run.run_id)
            worker = SimpleWorker([queue], connection=connection)
            worker.work(burst=True)
            assert len(queue.failed_job_registry.get_job_ids()) == 1
            assert len(queue.finished_job_registry.get_job_ids()) == 7
            assert latest(engine).status == 'completed_with_errors'
            assert 'burn:good' in calls
            # Exercise a real workhorse process death after saved global work.
            # Worker forks; only its child exits, and RQ records the termination.
            import os
            from rq import Worker
            def die(a):
                os._exit(17)
            monkeypatch.setattr(p, '_execute', die)
            retry, _ = p.claim_run('job')
            p.dispatch_next(retry.run_id)
            Worker([queue], connection=connection).work(burst=True)
            assert len(queue.failed_job_registry.get_job_ids()) == 2
            monkeypatch.setattr(execution, 'rq_state', lambda task: queue.fetch_job(task).get_status().value)
            execution.reconcile(retry.run_id)
            assert latest(engine).status == 'failed'
            monkeypatch.setattr(p, '_execute', execute)
            retry, _ = p.claim_run('job')
            p.dispatch_next(retry.run_id)
            SimpleWorker([queue], connection=connection).work(burst=True)
            assert latest(engine).status == 'completed'
            assert calls.count('transcribe') == 1
            assert calls.count('burn:good') == 1
        finally:
            server.terminate()
            server.wait(timeout=5)
