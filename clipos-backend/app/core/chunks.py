"""Versioned bounded ASR/selection checkpoints with absolute source timestamps.

Each core interval owns words by midpoint; padding supplies cross-boundary context.
An empty ASR result is a successful checkpoint, never a missing output.
"""
import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from sqlmodel import Session
from app.config import settings
from app.core.artifacts import write_json
from app.core.assets import ensure_source, source_unchanged
from app.core.media import ffmpeg
from app.models.job import engine
from app.models.source_import import SourceImport

ASR_VERSION = 'whisper-vad-absolute-v1'
SELECTION_VERSION = 'window-selection-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def windows(duration):
    overlap = min(settings.chunk_overlap_seconds, settings.chunk_seconds / 2)
    return [dict(index=i, start=i*settings.chunk_seconds, end=min(duration, (i+1)*settings.chunk_seconds),
                 extract_start=max(0, i*settings.chunk_seconds-overlap),
                 extract_end=min(duration, (i+1)*settings.chunk_seconds+overlap))
            for i in range(math.ceil(duration/settings.chunk_seconds))]


def selected_track(asset, db_engine=None):
    tracks = json.loads(asset.metadata_json)['audio_tracks']
    with Session(db_engine or engine) as session:
        request = session.get(SourceImport, asset.job_id)
        explicit = request.audio_stream_index if request else None
    if explicit is not None:
        if explicit not in [t['stream_index'] for t in tracks]:
            raise ValueError('Selected microphone track no longer exists')
        return explicit
    for track in tracks:
        if any(word in (track.get('title') or '').lower() for word in ('mic', 'microphone', 'voice', 'commentary')):
            return track['stream_index']
    return tracks[0]['stream_index'] if tracks else None


def context(job_id, stage, index, db_engine=None):
    asset = ensure_source(job_id, db_engine or engine)
    window = windows(json.loads(asset.metadata_json)['duration'])[int(index)]
    asr_inputs = dict(version=ASR_VERSION, source=asset.fingerprint, window=window,
                      track=selected_track(asset, db_engine), model=settings.asr_model, vad=settings.asr_vad)
    if stage == 'transcribe_chunk':
        inputs = asr_inputs
    else:
        from app.core.highlights import SYSTEM_PROMPT, PROVIDER_TIERS
        transcript = load(job_id, 'transcribe_chunk', index, db_engine)
        if transcript is None:
            raise ValueError('Selection requires a verified ASR chunk')
        inputs = dict(version=SELECTION_VERSION, asr=digest(asr_inputs), words=digest(transcript),
                      prompt=SYSTEM_PROMPT, providers=[t[2] for t in PROVIDER_TIERS],
                      max_chars=settings.analysis_max_chars, minimum=settings.clip_min_seconds,
                      maximum=settings.clip_max_seconds, count=settings.target_clip_count,
                      total_seconds=settings.candidate_total_seconds)
    key = digest(inputs)
    path = Path(settings.storage_raw)/job_id/'chunks'/stage/key/f'{index}.json'
    return asset, window, key, path


def load(job_id, stage, index, db_engine=None):
    try:
        _, _, key, path = context(job_id, stage, index, db_engine)
        payload = json.loads(path.read_text())
        if payload['key'] == key and digest(payload['data']) == payload['digest']:
            return payload['data']
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    return None


def checkpoint_signature(job_id, stage, index, db_engine=None):
    data = load(job_id, stage, index, db_engine)
    if data is None:
        raise ValueError('Chunk checkpoint is incomplete')
    return digest(data)


def publish(job_id, stage, index, data, db_engine=None):
    asset, window, key, path = context(job_id, stage, index, db_engine)
    source_unchanged(asset)
    write_json(path, dict(key=key, window=window, digest=digest(data), data=data))
    from app.core.cache import ensure_cache_capacity
    ensure_cache_capacity(db_engine or engine)
    return data


def assemble_transcript(job_id, db_engine=None):
    asset = ensure_source(job_id, db_engine or engine)
    available = {}
    for w in windows(json.loads(asset.metadata_json)['duration']):
        words = load(job_id, 'transcribe_chunk', str(w['index']), db_engine)
        if words is not None:
            available[w['index']] = words
    result = []
    for index, words in available.items():
        for word in words:
            owner = int(((word['start']+word['end'])/2)//settings.chunk_seconds)
            # Padding is available immediately for a boundary-crossing candidate;
            # prefer the owning chunk once that chunk has completed.
            if owner == index or owner not in available:
                result.append(word)
    result.sort(key=lambda word: (word['start'], word['end']))
    deduped = []
    for word in result:
        duplicate = next((old for old in reversed(deduped[-10:])
            if old['word'].strip().casefold() == word['word'].strip().casefold()
            and max(0, min(old['end'], word['end'])-max(old['start'], word['start'])) /
                min(old['end']-old['start'], word['end']-word['start']) > .5), None)
        if duplicate is None:
            deduped.append(word)
    result = deduped
    write_json(Path(settings.storage_raw)/job_id/'transcript.json', result)
    return result


def transcribe_chunk(job_id, index, db_engine=None):
    from app.core.cache import ensure_cache_capacity
    ensure_cache_capacity(db_engine or engine)
    cached = load(job_id, 'transcribe_chunk', index, db_engine)
    if cached is not None:
        assemble_transcript(job_id, db_engine)
        return cached
    asset, window, _, path = context(job_id, 'transcribe_chunk', index, db_engine)
    track = selected_track(asset, db_engine)
    words = []
    if track is not None:
        from app.core.transcribe import _call_whisper_asr
        path.parent.mkdir(parents=True, exist_ok=True)
        # Scratch PCM never survives a successful or failed unit.
        with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
            audio = str(Path(temporary)/'audio.wav')
            subprocess.run([ffmpeg(), '-y', '-v', 'error', '-ss', str(window['extract_start']),
                            '-i', asset.source_path, '-t', str(window['extract_end']-window['extract_start']),
                            '-map', f'0:{track}', '-vn', '-af', 'aresample=async=1:first_pts=0', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', audio],
                           check=True, capture_output=True, timeout=settings.media_timeout_seconds)
            for word in _call_whisper_asr(audio):
                local_start, local_end = float(word['start']), float(word['end'])
                if not math.isfinite(local_start) or not math.isfinite(local_end):
                    continue
                start = max(window['extract_start'], local_start+window['extract_start'])
                end = min(window['extract_end'], local_end+window['extract_start'])
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end and str(word['word']).strip():
                    words.append(dict(word=str(word['word']), start=round(start, 4), end=round(end, 4)))
    publish(job_id, 'transcribe_chunk', index, words, db_engine)
    assemble_transcript(job_id, db_engine)
    return words


def _bounded_groups(words):
    """No speech is dropped to fit a prompt; split dense windows by text budget."""
    groups, group, size = [], [], 0
    for word in words:
        # A single pathological token cannot bypass the per-request budget.
        word = dict(word, word=word['word'][:500])
        cost = len(word['word']) + 40
        if group and size + cost > settings.analysis_max_chars:
            groups.append(group)
            # Keep a small spoken context across text-budget splits as well as
            # across time windows, so setup/payoff can cross either boundary.
            context_words, size = [], 0
            for old in reversed(group):
                old_cost = len(old['word']) + 40
                if size + old_cost > min(2000, settings.analysis_max_chars//5):
                    break
                context_words.insert(0, old)
                size += old_cost
            group = context_words
        group.append(word)
        size += cost
    if group:
        groups.append(group)
    return groups


def _overlaps(a, b):
    intersection = max(0, min(a['end'], b['end'])-max(a['start'], b['start']))
    return intersection / min(a['end']-a['start'], b['end']-b['start']) > .5


def assemble_candidates(job_id, db_engine=None):
    asset = ensure_source(job_id, db_engine or engine)
    candidates = []
    for window in windows(json.loads(asset.metadata_json)['duration']):
        data = load(job_id, 'highlights_chunk', str(window['index']), db_engine)
        if data:
            candidates.extend(data)
    selected, seconds = [], 0
    for h in sorted(candidates, key=lambda h: (-h['virality_score'], h['start'], h['clip_id'])):
        if len(selected) >= settings.target_clip_count:
            break
        if seconds+h['end']-h['start'] > settings.candidate_total_seconds or any(_overlaps(h, old) for old in selected):
            continue
        selected.append(h)
        seconds += h['end']-h['start']
    write_json(Path(settings.storage_raw)/job_id/'highlights.json', selected)
    return selected


def highlights_chunk(job_id, index, db_engine=None):
    from app.core.cache import ensure_cache_capacity
    ensure_cache_capacity(db_engine or engine)
    cached = load(job_id, 'highlights_chunk', index, db_engine)
    if cached is not None:
        assemble_candidates(job_id, db_engine)
        return cached
    from app.core.highlights import (SYSTEM_PROMPT, PROVIDER_TIERS, _build_transcript_text,
                                     _parse_llm_json, _validate_highlights, call_llm_with_fallback)
    asset, window, key, _ = context(job_id, 'highlights_chunk', index, db_engine)
    words = load(job_id, 'transcribe_chunk', index, db_engine)
    candidates = []
    for group in _bounded_groups(words):
        raw, _ = call_llm_with_fallback(messages=[{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': 'Absolute source seconds. Select only inside this excerpt.\n'+_build_transcript_text(group)}],
            provider_tiers=PROVIDER_TIERS, temperature=.3, max_tokens=4096)
        candidates.extend(_validate_highlights(_parse_llm_json(raw)))
    if not words:
        # Eligibility only: Phase 5 adds visual evidence/ranking, never claim this is a detected highlight.
        length = min(settings.clip_max_seconds, window['end']-window['start'], 30)
        if length >= settings.clip_min_seconds:
            candidates = [dict(start=window['start'], end=window['start']+length,
                               hook_text='Gameplay moment', virality_score=0.0,
                               reasoning='No speech detected; visual candidate awaiting visual assessment.',
                               evidence='silent_unassessed')]
    valid = []
    for h in candidates:
        start, end, score = h['start'], h['end'], h['virality_score']
        if not all(math.isfinite(n) for n in (start, end, score)):
            continue
        if not window['extract_start'] <= start < end <= window['extract_end'] or not 0 <= score <= 1:
            continue
        if not settings.clip_min_seconds <= end-start <= settings.clip_max_seconds:
            continue
        h['clip_id'] = str(uuid5(NAMESPACE_URL, f'{asset.fingerprint}:{key}:{start:.3f}:{end:.3f}'))
        h['analysis_chunk'] = int(index)
        valid.append(h)
    if words and candidates and not valid:
        raise ValueError('Selection returned no candidates inside the configured source/duration bounds')
    publish(job_id, 'highlights_chunk', index, valid, db_engine)
    assemble_candidates(job_id, db_engine)
    return valid
