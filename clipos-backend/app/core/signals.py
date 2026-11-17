"""Optional cheap recall signals; loudness/cuts alone never establish clip quality."""
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
import numpy as np
from sqlmodel import Session, select
from app.config import settings
from app.core.media import ffmpeg
from app.models.selection import MomentMarker

SIGNAL_VERSION = 'audio-scene-markers-v1'


def signal_settings():
    return dict(version=SIGNAL_VERSION, audio=settings.selection_audio_enabled, scene=settings.selection_scene_enabled,
                peak_ratio=settings.selection_audio_peak_ratio, scene_threshold=settings.selection_scene_threshold,
                setup=settings.selection_setup_seconds, payoff=settings.selection_payoff_seconds,
                per_chunk=settings.selection_candidates_per_chunk)


def markers_for(asset, window, db_engine):
    with Session(db_engine) as session:
        return [dict(marker_id=m.marker_id, time_seconds=m.time_seconds, label=m.label)
                for m in session.exec(select(MomentMarker).where(MomentMarker.job_id == asset.job_id,
                    MomentMarker.source_fingerprint == asset.fingerprint,
                    MomentMarker.time_seconds >= window['start'], MomentMarker.time_seconds < window['end'])
                    .order_by(MomentMarker.time_seconds, MomentMarker.marker_id)).all()]


def _peaks(values, threshold, minimum_spacing):
    indexes = []
    for index in sorted(range(len(values)), key=lambda i: (-float(values[i]), i)):
        if values[index] < threshold:
            break
        if all(abs(index-old) >= minimum_spacing for old in indexes):
            indexes.append(index)
        if len(indexes) >= settings.selection_candidates_per_chunk:
            break
    return sorted(indexes)


def extract_events(asset, window, track):
    """Decode one bounded low-resolution chunk, using shared media executable."""
    events = []
    duration = window['extract_end']-window['extract_start']
    inputs = ['-ss', str(window['extract_start']), '-i', asset.source_path, '-t', str(duration)]
    with tempfile.TemporaryDirectory(prefix='clipos-signals-') as scratch:
        if settings.selection_audio_enabled and track is not None:
            audio = Path(scratch)/'audio.f32'
            subprocess.run([ffmpeg(), '-y', '-v', 'error', *inputs, '-map', f'0:{track}', '-vn',
                '-af', 'aresample=async=1:first_pts=0', '-ar', '8000', '-ac', '1', '-f', 'f32le', str(audio)],
                check=True, capture_output=True, timeout=settings.media_timeout_seconds)
            samples = np.fromfile(audio, dtype='<f4')
            block = 4000
            rms = np.array([np.sqrt(np.mean(samples[i:i+block]**2)) for i in range(0, len(samples), block)])
            baseline = max(float(np.median(rms)) if len(rms) else 0, .005)
            threshold = max(.025, baseline*settings.selection_audio_peak_ratio)
            for index in _peaks(rms, threshold, 10):
                events.append(dict(kind='audio_reaction', time=window['extract_start']+index*.5,
                                   strength=min(1., float(rms[index])/max(threshold*2, .001)),
                                   detail='Microphone energy peak; may be noise or excitement'))
        if settings.selection_scene_enabled:
            video = Path(scratch)/'frames.gray'
            meta = json.loads(asset.metadata_json)
            subprocess.run([ffmpeg(), '-y', '-v', 'error', *inputs, '-map', f"0:{meta['video_stream_index']}",
                '-vf', 'fps=1,scale=160:90', '-pix_fmt', 'gray', '-f', 'rawvideo', str(video)],
                check=True, capture_output=True, timeout=settings.media_timeout_seconds)
            data = np.fromfile(video, dtype=np.uint8)
            frames = data[:len(data)//14400*14400].reshape(-1, 14400).astype(np.float32)
            change = np.mean(np.abs(np.diff(frames, axis=0)), axis=1)/255 if len(frames)>1 else np.array([])
            for index in _peaks(change, settings.selection_scene_threshold, 10):
                events.append(dict(kind='scene_change', time=window['extract_start']+index+1,
                                   strength=float(change[index]), detail='Large visual transition; may be menu/cut/action'))
    return [event for event in events if window['start'] <= event['time'] < window['end']]


def event_candidates(asset, window, track, db_engine):
    events = extract_events(asset, window, track) if (settings.selection_audio_enabled or settings.selection_scene_enabled) else []
    events += [dict(kind='manual_marker', time=m['time_seconds'], strength=1., detail=m['label'] or 'Marked moment')
               for m in markers_for(asset, window, db_engine)]
    duration = json.loads(asset.metadata_json)['duration']
    candidates = []
    for event in events:
        start = max(0, event['time']-settings.selection_setup_seconds)
        end = min(duration, start+settings.clip_max_seconds, event['time']+settings.selection_payoff_seconds)
        if end-start < settings.clip_min_seconds:
            start = max(0, end-settings.clip_min_seconds)
            end = min(duration, max(end, start+settings.clip_min_seconds))
        if end-start < settings.clip_min_seconds:
            continue
        candidates.append(dict(clip_id=str(uuid5(NAMESPACE_URL, f"{asset.fingerprint}:{SIGNAL_VERSION}:{event['kind']}:{event['time']:.3f}")),
            start=round(start, 4), end=round(end, 4), hook_text='Marked moment' if event['kind']=='manual_marker' else 'Potential gameplay moment',
            virality_score=0., reasoning=event['detail'], evidence=[event['kind']], signals=[event],
            evidence_strength=event['strength'], selection_status='candidate'))
    return candidates


def merge_candidates(candidates):
    """Union coincident evidence only if a complete setup/payoff still fits budget."""
    merged = []
    for item in sorted(candidates, key=lambda h: (h['start'], h['end'], h['clip_id'])):
        item = dict(item)
        item['evidence'] = item.get('evidence', ['transcript'])
        if isinstance(item['evidence'], str): item['evidence'] = [item['evidence']]
        match = None
        for old in merged:
            overlap = max(0, min(item['end'], old['end'])-max(item['start'], old['start']))
            union = max(item['end'], old['end'])-min(item['start'], old['start'])
            if overlap/min(item['end']-item['start'], old['end']-old['start']) > .5 and union <= settings.clip_max_seconds:
                match = old
                break
        if match is None:
            merged.append(item)
        else:
            match['start'], match['end'] = min(match['start'], item['start']), max(match['end'], item['end'])
            if item['virality_score'] > match['virality_score']:
                match.update(hook_text=item['hook_text'], reasoning=item['reasoning'], virality_score=item['virality_score'])
            match['evidence'] = sorted(set(match['evidence']+item['evidence']))
            match['signals'] = match.get('signals', []) + item.get('signals', [])
            match['evidence_strength'] = max(match.get('evidence_strength', 0), item.get('evidence_strength', 0))
            match['merged_ids'] = sorted(set(match.get('merged_ids', [match['clip_id']])+item.get('merged_ids', [item['clip_id']])))
    return merged
