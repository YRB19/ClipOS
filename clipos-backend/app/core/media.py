"""One validated FFmpeg/ffprobe pair for every media operation."""
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from app.config import settings


def _inspect(ffmpeg: str, ffprobe: str) -> dict:
    def run(binary, *args):
        return subprocess.run([binary, *args], check=True, capture_output=True,
                              text=True, timeout=15).stdout
    version = run(ffmpeg, '-version').splitlines()[0]
    probe_version = run(ffprobe, '-version').splitlines()[0]
    if version.split()[2] != probe_version.split()[2]:
        raise RuntimeError('FFmpeg and ffprobe versions differ; configure a matching pair')
    filters = run(ffmpeg, '-filters')
    encoders = run(ffmpeg, '-encoders')
    for name, output in [('ass', filters), ('subtitles', filters),
                         ('libx264', encoders), ('aac', encoders)]:
        if not re.search(r'\s' + name + r'\s', output):
            raise RuntimeError(f'Missing required media capability: {name}')
    return dict(ffmpeg=ffmpeg, ffprobe=ffprobe, version=version,
                ffprobe_version=probe_version, ass=True, libx264=True, aac=True)


@lru_cache(maxsize=8)
def _resolve(explicit, probe, legacy):
    configured = explicit or legacy
    candidates = []
    if configured:
        path = Path(configured).expanduser()
        binary = path / 'ffmpeg' if path.is_dir() else path
        candidates.append((str(binary), probe or str(binary.with_name('ffprobe'))))
    elif probe:
        candidates.append((str(Path(probe).with_name('ffmpeg')), probe))
    else:
        for directory in ['/opt/homebrew/opt/ffmpeg-full/bin',
                          '/usr/local/opt/ffmpeg-full/bin']:
            candidates.append((f'{directory}/ffmpeg', f'{directory}/ffprobe'))
        binary = shutil.which('ffmpeg')
        if binary:
            candidates.append((binary, str(Path(binary).with_name('ffprobe'))))
    errors = []
    for binary, probe_binary in candidates:
        try:
            return _inspect(binary, probe_binary)
        except (OSError, subprocess.SubprocessError, RuntimeError, IndexError) as exc:
            errors.append(f'{binary}: {exc}')
    raise RuntimeError('No working FFmpeg/ffprobe pair with ASS, libx264 and AAC. '
                       'Set FFMPEG_PATH and FFPROBE_PATH to matching executables '
                       '(or FFMPEG_FULL_PATH to the executable/bin directory). '
                       + '; '.join(errors))


def media_diagnostics() -> dict:
    return _resolve(settings.ffmpeg_path or None, settings.ffprobe_path or None, settings.ffmpeg_full_path or None)


def ffmpeg() -> str:
    return media_diagnostics()['ffmpeg']


def ffprobe() -> str:
    return media_diagnostics()['ffprobe']
