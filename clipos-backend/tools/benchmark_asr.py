"""Run one cold ASR probe in this Python environment with required word timings.

Invoke once with the backend venv for faster-whisper, once with an isolated
mlx-whisper venv on the same WAV. A reference transcript is optional; without it
WER is unknown. This never edits a job, cache or source file.
"""
import argparse
import json
import re
import resource
import time
import wave
from pathlib import Path


def _wer(reference, predicted):
    a, b = re.findall(r"[a-z0-9]+", reference.lower()), re.findall(r"[a-z0-9]+", predicted.lower())
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1] / len(a) if a else None


def probe(audio, adapter, model, truth=None):
    began = time.monotonic()
    if adapter == 'faster-whisper':
        from faster_whisper import WhisperModel
        engine = WhisperModel(model, device='cpu', compute_type='int8', local_files_only=True)
        loaded = time.monotonic()
        segments, _ = engine.transcribe(str(audio), word_timestamps=True, vad_filter=True)
        words = [dict(word=w.word, start=float(w.start), end=float(w.end))
                 for segment in segments for w in (segment.words or [])]
    else:
        import mlx_whisper
        loaded = time.monotonic()  # MLX loads lazily inside transcribe().
        result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model, word_timestamps=True, verbose=False)
        words = [dict(word=w['word'], start=float(w['start']), end=float(w['end']))
                 for segment in result['segments'] for w in segment.get('words', [])]
    ended = time.monotonic()
    with wave.open(str(audio)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    invalid_times = sum(not 0 <= w['start'] < w['end'] <= duration + .25 for w in words)
    transcript = ' '.join(w['word'].strip() for w in words)
    return dict(adapter=adapter, model=model, audio=str(audio.resolve()), audio_seconds=round(duration, 3),
                load_seconds=round(loaded - began, 3) if adapter == 'faster-whisper' else None,
                total_seconds=round(ended - began, 3), realtime_factor=round((ended - began) / duration, 3),
                peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                word_count=len(words), invalid_word_times=invalid_times, transcript=transcript, words=words,
                wer=_wer(truth, transcript) if truth else None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('--adapter', choices=['faster-whisper', 'mlx-whisper'], required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--truth', default=None)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.audio, args.adapter, args.model, args.truth)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in ('words',)}, indent=2))
