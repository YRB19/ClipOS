"""Real short-media checks, plus binary selection compatibility/diagnostics."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from app.config import settings
from app.core import media
from app.core.ffprobe import get_video_metadata
from app.core.transcribe import _extract_audio
from app.core.clip_cutter import cut_clip
from app.core.reframe import reframe_clip
from app.core.captions import generate_captions, burn_captions


def test_legacy_executable_and_directory_resolve_to_same_pair():
    diagnostics = media.media_diagnostics()
    binary = diagnostics['ffmpeg']
    assert media._resolve(None, None, binary)['ffprobe'] == diagnostics['ffprobe']
    assert media._resolve(None, None, str(Path(binary).parent))['ffmpeg'] == binary


def test_explicit_bad_binary_has_actionable_error():
    with pytest.raises(RuntimeError, match='Set FFMPEG_PATH and FFPROBE_PATH'):
        media._resolve('/missing/ffmpeg', None, None)


def test_missing_ass_capability_is_rejected():
    def output(binary, *args):
        if args == ('-version',):
            return 'ffmpeg version 1.0'
        return ' libx264 encoder\n aac encoder\n'
    with patch('app.core.media.subprocess.run') as run:
        run.side_effect = lambda args, **kwargs: type('Result', (), {'stdout': output(*args)})()
        with pytest.raises(RuntimeError, match='Missing required media capability: ass'):
            media._inspect('test-ffmpeg', 'test-ffprobe')


def test_all_real_media_operations_use_working_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'storage_raw', str(tmp_path / 'raw'))
    monkeypatch.setattr(settings, 'storage_clips', str(tmp_path / 'clips'))
    raw = Path(settings.storage_raw) / 'fixture'
    raw.mkdir(parents=True)
    source = raw / 'source.mp4'
    subprocess.run([media.ffmpeg(), '-y', '-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-c:a', 'aac', str(source)],
                   check=True, capture_output=True, timeout=30)
    _extract_audio(str(source), str(raw / 'audio.wav'))
    assert (raw / 'audio.wav').stat().st_size > 1000
    (raw / 'transcript.json').write_text(json.dumps([dict(word='Hello', start=0.1, end=0.8)]))
    (raw / 'highlights.json').write_text(json.dumps([dict(clip_id='clip', start=-0.1, end=1.2,
                                                       hook_text='fixture', virality_score=0.5, reasoning='fixture')]))
    cut = cut_clip('fixture', 'clip')
    assert 1.1 <= cut['duration'] <= 1.4
    assert json.loads((raw / 'highlights.json').read_text())[0]['start'] == 0.0
    monkeypatch.setattr('app.core.reframe._detect_faces_on_frames', lambda _: [])
    reframe_clip('fixture', 'clip')
    generate_captions('fixture', 'clip')
    final = burn_captions('fixture', 'clip')
    metadata = get_video_metadata(final)
    assert (metadata['width'], metadata['height']) == (1080, 1920)
    assert 1.1 <= metadata['duration'] <= 1.4
    streams = json.loads(subprocess.run([media.ffprobe(), '-v', 'error', '-show_streams',
                                         '-of', 'json', final], check=True,
                                        capture_output=True, text=True, timeout=30).stdout)['streams']
    assert {'audio', 'video'} <= {s['codec_type'] for s in streams}
    durations = [float(s['duration']) for s in streams if s['codec_type'] in ('video', 'audio')]
    assert max(durations) - min(durations) < 0.15
    assert 'Dialogue:' in (Path(final).parent / 'captions.ass').read_text()
