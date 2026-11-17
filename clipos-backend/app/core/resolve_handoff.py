"""Optional source-linked OTIO handoff; the automatic render path is unchanged."""
import json
import math
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import opentimelineio as otio
from sqlmodel import Session, select

from app.config import settings
from app.core.assets import ensure_source, source_unchanged
from app.core.recipes import EditRecipe, default_recipe, read_edit, validate_source
from app.models.selection import MomentMarker
from app.models.job import engine


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole, ms = divmod(remainder, 1000)
    return f'{hours:02}:{minutes:02}:{whole:02},{ms:03}'


def _srt(words: list[dict], offset: float = 0) -> str:
    lines = []
    for index, word in enumerate(words, 1):
        lines.extend((str(index), f"{_srt_time(word['start'] + offset)} --> {_srt_time(word['end'] + offset)}", word['word'].replace('\n', ' ').replace('\r', ' ').strip(), ''))
    return '\n'.join(lines) + ('\n' if lines else '')


def export_handoff(job_id: str, clip_ids: list[str] | None = None, db_engine=None) -> dict:
    """Export saved edits, or defaults, without creating or changing edit revisions."""
    db_engine = db_engine or engine
    selected_path = Path(settings.storage_raw)/job_id/'highlights.json'
    if not selected_path.is_file():
        raise FileNotFoundError('Selected highlights are unavailable')
    selected = json.loads(selected_path.read_text())
    by_id = {item['clip_id']: item for item in selected}
    if clip_ids is None:
        clip_ids = list(by_id)
    if not clip_ids or len(clip_ids) != len(set(clip_ids)) or any(item not in by_id for item in clip_ids):
        raise ValueError('Select at least one unique clip ID from this job shortlist')
    asset = ensure_source(job_id, db_engine)
    metadata = json.loads(asset.metadata_json)
    try:
        rate = Fraction(metadata['frame_rate'])
    except (ValueError, ZeroDivisionError, KeyError) as exc:
        raise ValueError('Source frame rate is unavailable for interchange') from exc
    if not 1 <= rate <= 240:
        raise ValueError('Source frame rate is unavailable for interchange')
    fps = float(rate)
    with Session(db_engine) as session:
        markers = session.exec(select(MomentMarker).where(MomentMarker.job_id == job_id,
            MomentMarker.source_fingerprint == asset.fingerprint).order_by(MomentMarker.time_seconds)).all()
    timeline = otio.schema.Timeline(name=f'ClipOS {job_id[:8]} source shortlist')
    timeline.metadata['clipos'] = {'job_id': job_id, 'source_fingerprint': asset.fingerprint,
                                    'source_frame_rate': str(rate)}
    track = otio.schema.Track(name='Source video', kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)
    audio_track = None
    if metadata['audio_tracks']:
        audio_track = otio.schema.Track(name='Source audio (default stream)', kind=otio.schema.TrackKind.Audio)
        timeline.tracks.append(audio_track)
    manifest = {'schema_version': 1, 'job_id': job_id, 'source_path': asset.source_path,
                'source_uri': Path(asset.source_path).as_uri(), 'source_fingerprint': asset.fingerprint,
                'source_frame_rate': str(rate), 'clips': [], 'limitations': [
                    'OTIO source ranges are quantized to source frames; variable-frame-rate frame accuracy requires Resolve inspection.',
                    'ClipOS layout, animated captions, audio stream selection/gain and effects do not transfer automatically.']}
    folder = Path(settings.storage_clips)/job_id/'handoffs'/str(uuid4())
    folder.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.handoff-', dir=folder.parent))
    combined_words = []
    timeline_seconds = 0.0
    try:
        for position, clip_id in enumerate(clip_ids, 1):
            edit = read_edit(job_id, clip_id, db_engine)
            recipe = EditRecipe.model_validate(edit['recipe']) if edit else default_recipe(job_id, clip_id, asset, db_engine)
            validate_source(recipe, asset, job_id)
            name = f'{position:02d}'
            intervals = []
            requested_offset = 0.0
            clip_start = timeline_seconds
            mapped_words = []
            for part, interval in enumerate(recipe.intervals, 1):
                first = math.floor(interval.start * fps + 1e-8)
                last = math.ceil(interval.end * fps - 1e-8)
                last = max(last, first + 1)
                source_range = otio.opentime.TimeRange(otio.opentime.RationalTime(first, fps),
                                                       otio.opentime.RationalTime(last-first, fps))
                item = otio.schema.Clip(name=f'{name} part {part}',
                    media_reference=otio.schema.ExternalReference(target_url=Path(asset.source_path).as_uri()),
                    source_range=source_range)
                item.metadata['clipos'] = {'clip_id': clip_id, 'revision_id': edit['revision_id'] if edit else None,
                                           'part': part, 'requested_start': interval.start, 'requested_end': interval.end}
                for marker in markers:
                    if interval.start <= marker.time_seconds < interval.end:
                        frame = max(0, min(last-first-1, round(marker.time_seconds * fps)-first))
                        item.markers.append(otio.schema.Marker(name=marker.label or 'ClipOS marker',
                            marked_range=otio.opentime.TimeRange(otio.opentime.RationalTime(frame, fps),
                                                                 otio.opentime.RationalTime(1, fps)),
                            color=otio.schema.MarkerColor.RED))
                track.append(item)
                if audio_track is not None:
                    audio_track.append(otio.schema.Clip(name=f'{name} audio part {part}',
                        media_reference=otio.schema.ExternalReference(target_url=Path(asset.source_path).as_uri()),
                        source_range=source_range))
                intervals.append({'requested_start': interval.start, 'requested_end': interval.end,
                                  'first_frame': first, 'end_frame_exclusive': last,
                                  'timeline_start_seconds': timeline_seconds})
                if recipe.captions.enabled:
                    for word in recipe.captions.words:
                        local_start = max(word.start, requested_offset)
                        local_end = min(word.end, requested_offset + interval.end - interval.start)
                        if local_end > local_start:
                            mapped_words.append({'word': word.word,
                                'start': timeline_seconds-clip_start+local_start-requested_offset,
                                'end': timeline_seconds-clip_start+local_end-requested_offset})
                requested_offset += interval.end-interval.start
                timeline_seconds += (last-first)/fps
            (staging/f'{name}.srt').write_text(_srt(mapped_words), encoding='utf-8')
            combined_words.extend({**w, 'start': w['start']+clip_start, 'end': w['end']+clip_start} for w in mapped_words)
            manifest['clips'].append({'clip_id': clip_id, 'revision_id': edit['revision_id'] if edit else None,
                'hook_text': by_id[clip_id].get('hook_text', ''), 'intervals': intervals,
                'caption_file': f'{name}.srt', 'layout': recipe.layout.model_dump(),
                'audio': recipe.audio.model_dump(), 'export': recipe.export.model_dump()})
        (staging/'timeline.srt').write_text(_srt(combined_words), encoding='utf-8')
        (staging/'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        otio.adapters.write_to_file(timeline, str(staging/'timeline.otio'), adapter_name='otio_json')
        imported = otio.adapters.read_from_file(str(staging/'timeline.otio'))
        expected = sum(len(c['intervals']) for c in manifest['clips'])
        if len(imported.tracks[0]) != expected or (audio_track is not None and len(imported.tracks[1]) != expected):
            raise ValueError('OTIO read-back did not preserve all source intervals')
        source_unchanged(asset)
        staging.rename(folder)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    base = '/files/clips/' + folder.relative_to(Path(settings.storage_clips)).as_posix()
    return {'handoff_id': folder.name, 'timeline_url': base+'/timeline.otio',
            'manifest_url': base+'/manifest.json', 'srt_url': base+'/timeline.srt',
            'clip_count': len(manifest['clips']), 'source_path': asset.source_path,
            'folder_path': str(folder.resolve())}
