"""Isolated render comparison on a short source interval; never touches the library.

Run from clipos-backend with PYTHONPATH=. and an explicit output report. The
lossless FFV1 result is a reference for encoder loss after ClipOS composition;
SSIM is a screening metric, not a human judgment of HUD/caption readability.
"""
import argparse
import json
import re
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from app.core.ffprobe import get_video_metadata
from app.core.media import ffmpeg
from app.core.recipes import EditRecipe, SourceReference, Interval, CaptionEdit, CaptionWord, ExportPreset
from app.core.renderer import build_command, recipe_ass, validate_output
from app.core.artifacts import write_json


def _encode(command, encoder, output, bitrate):
    args = command[:]
    index = args.index('-c:v')
    args[index + 1] = encoder
    for option in ('-preset', '-crf'):
        index = args.index(option)
        del args[index:index + 2]
    if encoder == 'libx264':
        args[args.index('-c:v') + 2:args.index('-c:v') + 2] = ['-preset', 'fast', '-crf', '20']
    elif encoder == 'h264_videotoolbox':
        args[args.index('-c:v') + 2:args.index('-c:v') + 2] = ['-b:v', str(bitrate), '-maxrate', str(bitrate * 2),
                                                              '-bufsize', str(bitrate * 2), '-allow_sw', '0']
    args[-1] = str(output)
    return args


def _run(command, cwd):
    # macOS /usr/bin/time -l measures each FFmpeg process separately.
    start = time.monotonic()
    result = subprocess.run(['/usr/bin/time', '-l', *command], cwd=cwd, capture_output=True, text=True,
                            timeout=600, check=False)
    if result.returncode:
        raise RuntimeError(f'Benchmark FFmpeg failed ({result.returncode}): {result.stderr[-1200:]}')
    resident = re.search(r'(\d+)\s+maximum resident set size', result.stderr)
    return dict(wall_seconds=round(time.monotonic() - start, 3),
                peak_rss_bytes=int(resident.group(1)) if resident else None)


def _ssim(candidate, reference):
    result = subprocess.run([ffmpeg(), '-v', 'info', '-i', str(candidate), '-i', str(reference),
                             '-filter_complex', '[0:v][1:v]ssim', '-an', '-f', 'null', '-'],
                            check=True, capture_output=True, text=True, timeout=300)
    matches = re.findall(r'All:([0-9.]+)', result.stderr)
    if not matches:
        raise ValueError('FFmpeg did not report SSIM')
    return float(matches[-1])


def compare(source, start, duration, rate, bitrate, output):
    source = source.resolve()
    meta = get_video_metadata(str(source))
    if start < 0 or duration <= 0 or duration > 30 or start + duration > meta['duration']:
        raise ValueError('Select an interval within the source and at most 30 seconds')
    recipe = EditRecipe(source=SourceReference(job_id='benchmark', fingerprint='0' * 64),
                        intervals=[Interval(start=start, end=start + duration)],
                        captions=CaptionEdit(words=[CaptionWord(word='ClipOS benchmark', start=1, end=min(2, duration))])
                        if duration >= 2 else CaptionEdit(enabled=False),
                        export=ExportPreset(frame_rate=rate))
    report = dict(source=str(source), source_duration_seconds=meta['duration'], interval=[start, start + duration],
                  frame_rate=recipe.export.frame_rate, video_bitrate_target=bitrate,
                  hardware='local', results={})
    with tempfile.TemporaryDirectory(prefix='clipos-phase6-render-') as temporary:
        folder = Path(temporary)
        (folder/'captions.ass').write_text(recipe_ass(recipe))
        reference = folder/'reference.mkv'
        base = build_command(recipe, source, meta, 'final', reference)
        ref_command = _encode(base, 'ffv1', reference, bitrate)
        report['reference'] = _run(ref_command, folder)
        for encoder in ('libx264', 'h264_videotoolbox'):
            target = folder/f'{encoder}.mp4'
            command = _encode(base, encoder, target, bitrate)
            metrics = _run(command, folder)
            verified = validate_output(target, recipe, 'final')
            metrics.update(size_bytes=target.stat().st_size, render_fps=round(duration/metrics['wall_seconds'] * float(Fraction(recipe.export.frame_rate)), 2),
                           realtime_factor=round(duration/metrics['wall_seconds'], 3),
                           ssim_to_lossless=_ssim(target, reference),
                           actual_frame_rate=verified['frame_rate'], codec=verified['codec'],
                           color=verified['color'])
            report['results'][encoder] = metrics
    write_json(output, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--seconds', type=float, default=6)
    parser.add_argument('--fps', default='30/1')
    parser.add_argument('--videotoolbox-bitrate', type=int, default=8_000_000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.source, args.start, args.seconds, args.fps,
                             args.videotoolbox_bitrate, args.output), indent=2))
