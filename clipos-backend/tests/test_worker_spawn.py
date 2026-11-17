"""Mac worker trial against private Redis; no shared queue or library state."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from redis import Redis
from rq import Queue, SpawnWorker, Worker

from worker import worker_class


def test_worker_selection_matches_connection_support():
    assert worker_class('unix:///tmp/redis.sock') is Worker
    assert worker_class('redis://127.0.0.1:6379') is (SpawnWorker if sys.platform == 'darwin' else Worker)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS spawn worker integration')
def test_spawn_worker_completes_job_with_scheduler_and_tcp(monkeypatch):
    binary = shutil.which('redis-server')
    if not binary:
        pytest.skip('redis-server unavailable')
    # pytest's runtime fixture changes cwd, so give the spawned interpreter the
    # backend import root explicitly. The job itself has no media/database I/O.
    backend = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH', str(backend) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    with tempfile.TemporaryDirectory(prefix='clipos-spawn-rq-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        server = subprocess.Popen([binary, '--bind', '127.0.0.1', '--port', str(port),
                                   '--save', '', '--appendonly', 'no', '--dir', directory],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            connection = Redis(host='127.0.0.1', port=port)
            for _ in range(100):
                try:
                    if connection.ping():
                        break
                except Exception:
                    time.sleep(.02)
            else:
                pytest.fail('Private Redis did not start')
            queue = Queue(connection=connection)
            job = queue.enqueue('app.core.pipeline._key', 'rank', None)
            SpawnWorker([queue], connection=connection).work(burst=True, with_scheduler=True)
            job.refresh()
            assert job.is_finished and job.return_value() == 'rank'
            assert queue.finished_job_registry.get_job_ids() == [job.id]
        finally:
            server.terminate()
            server.wait(timeout=5)
