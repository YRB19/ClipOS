"""Tests never write to the local library, call providers or use shared Redis."""
import importlib
from pathlib import Path

import pytest
from sqlmodel import create_engine


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    from app.models.job import init_db
    from app.config import settings
    from app.core.media import ffmpeg
    # Legacy media fixtures invoke bare ffmpeg; use the same validated pair.
    import os
    monkeypatch.setenv('PATH', str(Path(ffmpeg()).parent) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'storage').mkdir(exist_ok=True)
    monkeypatch.setattr(settings, 'storage_raw', './storage/raw')
    monkeypatch.setattr(settings, 'storage_clips', './storage/clips')
    engine = create_engine(f'sqlite:///{tmp_path}/isolated-runtime.db', connect_args={'check_same_thread': False})
    init_db(engine)
    for name in ['app.models.job', 'app.core.transcribe', 'app.core.highlights', 'app.core.scoring',
                 'app.core.clip_cutter', 'app.core.pipeline', 'app.core.execution',
                 'app.core.recipes', 'app.core.assets', 'app.core.renders']:
        monkeypatch.setattr(importlib.import_module(name), 'engine', engine)
    from app.main import app
    for route in app.routes:
        if getattr(route, 'name', None) in ('raw-files', 'clip-files'):
            directory = str(tmp_path/'storage'/('raw' if route.name == 'raw-files' else 'clips'))
            Path(directory).mkdir(parents=True, exist_ok=True)
            monkeypatch.setattr(route.app, 'directory', directory)
            monkeypatch.setattr(route.app, 'all_directories', [directory])
    monkeypatch.setattr('app.main.init_db', lambda: init_db(engine))
    monkeypatch.setattr('app.core.execution.reconcile_active', lambda: None)
    def no_network(*args, **kwargs):
        raise AssertionError('External HTTP provider calls are forbidden in tests')
    monkeypatch.setattr('requests.sessions.Session.request', no_network)
    yield
    engine.dispose()
