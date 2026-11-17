"""Version 1 recipes: normalized source coordinates; caption/keyframe time is output-local."""
import json
from fractions import Fraction
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from app.config import settings
from app.models.job import engine
from app.models.edit import ClipEdit, EditRevision
from app.models.execution import now


class RecipeModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class SourceReference(RecipeModel):
    job_id: str
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class Interval(RecipeModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Source interval end must follow start')
        return self


class Region(RecipeModel):
    x: float = Field(default=0, ge=0, lt=1)
    y: float = Field(default=0, ge=0, lt=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)
    @model_validator(mode='after')
    def bounds(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError('Source region must fit within normalized [0,1] coordinates')
        return self


class CropKeyframe(RecipeModel):
    time: float = Field(ge=0)
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)


class Layout(RecipeModel):
    mode: Literal['fit_background', 'gameplay_facecam', 'manual_crop'] = 'fit_background'
    gameplay: Region = Field(default_factory=Region)
    facecam: Region | None = None
    facecam_height: float = Field(default=0.28, ge=0.15, le=0.5)
    crop_keyframes: list[CropKeyframe] = Field(default_factory=list, max_length=120)
    background: str = Field(default='#101018', pattern=r'^#[0-9a-fA-F]{6}$')
    @model_validator(mode='after')
    def layout_valid(self):
        if self.mode == 'gameplay_facecam' and self.facecam is None:
            raise ValueError('A facecam source region is required for gameplay_facecam')
        if self.crop_keyframes and self.mode != 'manual_crop':
            raise ValueError('Crop keyframes apply only to manual_crop')
        times = [k.time for k in self.crop_keyframes]
        if times != sorted(set(times)):
            raise ValueError('Crop keyframe times must increase strictly')
        for k in self.crop_keyframes:
            if k.x + self.gameplay.width > 1.000001 or k.y + self.gameplay.height > 1.000001:
                raise ValueError('Keyframe crop must stay inside the source')
        return self


class CaptionWord(RecipeModel):
    word: str = Field(max_length=200)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Caption end must follow start')
        return self


class CaptionStyle(RecipeModel):
    font: str = Field(default='Arial', pattern=r'^[\w -]{1,60}$')
    font_size: int = Field(default=80, ge=24, le=140)
    color: str = Field(default='#FFFFFF', pattern=r'^#[0-9a-fA-F]{6}$')
    highlight_color: str = Field(default='#FFFF00', pattern=r'^#[0-9a-fA-F]{6}$')
    margin_bottom: int = Field(default=220, ge=80, le=700)


class CaptionEdit(RecipeModel):
    enabled: bool = True
    words: list[CaptionWord] = Field(default_factory=list, max_length=5000)
    style: CaptionStyle = Field(default_factory=CaptionStyle)


class AudioTrack(RecipeModel):
    stream_index: int = Field(ge=0)
    gain: float = Field(default=1, ge=0, le=4)


class AudioEdit(RecipeModel):
    tracks: list[AudioTrack] = Field(default_factory=list, max_length=8)
    @model_validator(mode='after')
    def unique(self):
        if len({t.stream_index for t in self.tracks}) != len(self.tracks):
            raise ValueError('Each audio stream can be selected once')
        return self


class ExportPreset(RecipeModel):
    width: Literal[1080] = 1080
    height: Literal[1920] = 1920
    frame_rate: str = Field(default='30/1', max_length=64)
    codec: Literal['libx264'] = 'libx264'
    pixel_format: Literal['yuv420p'] = 'yuv420p'
    crf: int = Field(default=20, ge=16, le=28)
    preset: Literal['fast', 'medium'] = 'fast'
    @model_validator(mode='after')
    def rate(self):
        try:
            rate = Fraction(self.frame_rate)
            if not 1 <= rate <= 60 or max(rate.numerator, rate.denominator) > 2147483647:
                raise ValueError()
        except (ValueError, ZeroDivisionError):
            raise ValueError('Frame rate must be a rational number between 1 and 60 fps')
        self.frame_rate = f'{rate.numerator}/{rate.denominator}'
        return self


class EditRecipe(RecipeModel):
    schema_version: Literal[1] = 1
    source: SourceReference
    intervals: list[Interval] = Field(min_length=1, max_length=16)
    layout: Layout = Field(default_factory=Layout)
    captions: CaptionEdit = Field(default_factory=CaptionEdit)
    audio: AudioEdit = Field(default_factory=AudioEdit)
    export: ExportPreset = Field(default_factory=ExportPreset)
    @property
    def duration(self):
        return sum(i.end-i.start for i in self.intervals)
    @model_validator(mode='after')
    def timeline(self):
        if self.duration > 300:
            raise ValueError('A recipe supports at most 300 seconds of output')
        if any(w.end > self.duration+0.0001 for w in self.captions.words):
            raise ValueError('Caption times must fit the output timeline')
        if any(k.time > self.duration for k in self.layout.crop_keyframes):
            raise ValueError('Crop keyframes must fit the output timeline')
        return self


class RevisionConflict(Exception):
    pass


def validate_source(recipe, asset, job_id):
    if recipe.source.job_id != job_id or recipe.source.fingerprint != asset.fingerprint:
        raise ValueError('Recipe source does not match this job or its current media fingerprint')
    meta = json.loads(asset.metadata_json)
    if any(i.end > meta['duration']+0.0001 for i in recipe.intervals):
        raise ValueError('Source intervals exceed the original media duration')
    indexes = {t['stream_index'] for t in meta['audio_tracks']}
    if any(t.stream_index not in indexes for t in recipe.audio.tracks):
        raise ValueError('Selected audio stream is unavailable in this source')
    for region in [recipe.layout.gameplay, recipe.layout.facecam]:
        if region and (region.width*meta['width'] < 2 or region.height*meta['height'] < 2):
            raise ValueError('Source regions must span at least two pixels')


def default_recipe(job_id, clip_id, asset, db_engine=None):
    highlights = json.loads((Path(settings.storage_raw)/job_id/'highlights.json').read_text())
    candidate = next((h for h in highlights if h['clip_id'] == clip_id), None)
    if candidate is None:
        raise FileNotFoundError('Clip candidate not found')
    meta = json.loads(asset.metadata_json)
    interval = Interval(start=max(0, candidate['start']), end=min(meta['duration'], candidate['end']))
    transcript = Path(settings.storage_raw)/job_id/'transcript.json'
    words = json.loads(transcript.read_text()) if transcript.exists() else []
    captions = [CaptionWord(word=w['word'], start=max(0, w['start']-interval.start),
                            end=min(interval.end-interval.start, w['end']-interval.start))
                for w in words if w['end'] > interval.start and w['start'] < interval.end]
    try:
        rate = Fraction(meta['frame_rate'])
        rate = min(rate, Fraction(60)) if rate >= 1 else Fraction(30)
    except (ValueError, ZeroDivisionError):
        rate = Fraction(30)
    tracks = [AudioTrack(stream_index=meta['audio_tracks'][0]['stream_index'])] if meta['audio_tracks'] else []
    if tracks:
        from app.core.chunks import selected_track
        microphone = selected_track(asset, db_engine or engine)
        first_title = (meta['audio_tracks'][0].get('title') or '').lower()
        # OBS-style explicitly separate game/mic tracks need both in the export.
        # For unnamed/composite tracks retain the conventional first-track mix.
        if microphone != tracks[0].stream_index and any(label in first_title for label in ('game', 'desktop')):
            tracks[0].gain = .5
            tracks.append(AudioTrack(stream_index=microphone))
    return EditRecipe(source=SourceReference(job_id=job_id, fingerprint=asset.fingerprint),
                      intervals=[interval], captions=CaptionEdit(words=captions, enabled=bool(captions)),
                      audio=AudioEdit(tracks=tracks),
                      export=ExportPreset(frame_rate=str(rate)))


def read_edit(job_id, clip_id, db_engine=None):
    with Session(db_engine or engine) as session:
        head = session.get(ClipEdit, f'{job_id}:{clip_id}')
        if not head:
            return None
        revision = session.get(EditRevision, head.revision_id)
        return dict(revision=head.revision, revision_id=head.revision_id,
                    recipe=json.loads(revision.recipe_json))


def save_edit(job_id, clip_id, recipe, expected_revision, db_engine=None, generated_signature=None):
    from app.core.assets import ensure_source
    db_engine = db_engine or engine
    asset = ensure_source(job_id, db_engine)
    validate_source(recipe, asset, job_id)
    # Clip membership is checked independently of recipe contents/source paths.
    candidates = json.loads((Path(settings.storage_raw)/job_id/'highlights.json').read_text())
    if clip_id not in {h['clip_id'] for h in candidates}:
        raise FileNotFoundError('Clip candidate not found')
    key, revision_id = f'{job_id}:{clip_id}', str(uuid4())
    with Session(db_engine) as session:
        if expected_revision == 0:
            session.add(ClipEdit(key=key, job_id=job_id, clip_id=clip_id, revision=1, revision_id=revision_id,
                                 generated_signature=generated_signature))
        else:
            result = session.execute(update(ClipEdit).where(ClipEdit.key == key, ClipEdit.revision == expected_revision)
                                     .values(revision=expected_revision+1, revision_id=revision_id, updated_at=now(),
                                             generated_signature=generated_signature))
            if not result.rowcount:
                session.rollback()
                raise RevisionConflict('Edit changed; reload the current revision before saving')
        session.add(EditRevision(revision_id=revision_id, edit_key=key, revision=expected_revision+1,
                                 recipe_json=recipe.model_dump_json()))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise RevisionConflict('Edit already exists; reload its current revision') from exc
    return read_edit(job_id, clip_id, db_engine)


def ensure_edit(job_id, clip_id, db_engine=None):
    db_engine = db_engine or engine
    existing = read_edit(job_id, clip_id, db_engine)
    if existing:
        with Session(db_engine) as session:
            head = session.get(ClipEdit, f'{job_id}:{clip_id}')
            if head.generated_signature is None:
                return existing  # Human edits and pre-migration revisions are protected.
    from app.core.assets import ensure_source
    recipe = default_recipe(job_id, clip_id, ensure_source(job_id, db_engine), db_engine)
    import hashlib
    generated = hashlib.sha256(recipe.model_dump_json().encode()).hexdigest()
    if existing and head.generated_signature == generated:
        return existing
    try:
        return save_edit(job_id, clip_id, recipe, existing['revision'] if existing else 0, db_engine,
                         generated_signature=generated)
    except RevisionConflict:
        return read_edit(job_id, clip_id, db_engine)
