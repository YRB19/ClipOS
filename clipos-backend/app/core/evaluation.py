"""Offline paired selection evaluation against manually marked source intervals.

Usage: python -m app.core.evaluation manifest.json --output comparison.json
No inference, downloads or provider calls. Each recording supplies the same truth
for both modes; missing human feedback/time is unknown, never an inferred zero.
"""
import argparse
import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.core.artifacts import write_json


class Interval(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start: raise ValueError('Interval must have positive length')
        return self


class Prediction(Interval):
    clip_id: str
    accepted: bool | None = Field(default=None, strict=True)


class Mode(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    predictions: list[Prediction]
    review_seconds: float | None = Field(default=None, ge=0)
    @model_validator(mode='after')
    def unique(self):
        if len({p.clip_id for p in self.predictions}) != len(self.predictions): raise ValueError('Duplicate clip ID')
        return self


class Recording(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    recording_id: str
    duration_seconds: float = Field(gt=0)
    truth: list[Interval]
    transcript_only: Mode
    combined: Mode
    @model_validator(mode='after')
    def bounds(self):
        if any(p.end > self.duration_seconds for p in self.truth+self.transcript_only.predictions+self.combined.predictions):
            raise ValueError('Evaluation interval outside recording')
        return self


def metrics(truth, mode):
    edges = []
    for i, prediction in enumerate(mode.predictions):
        for j, moment in enumerate(truth):
            overlap = max(0, min(prediction.end, moment.end)-max(prediction.start, moment.start))
            if overlap/(moment.end-moment.start) >= .5:
                union = max(prediction.end, moment.end)-min(prediction.start, moment.start)
                edges.append((overlap/union, i, j))
    # Maximum bipartite matching: one broad clip cannot count as several detected
    # moments, nor can multiple replays inflate recall for one marked moment.
    adjacency = {i: [] for i in range(len(mode.predictions))}
    for score, i, j in sorted(edges, reverse=True): adjacency[i].append(j)
    matches = {}
    def augment(i, seen):
        for j in adjacency[i]:
            if j in seen: continue
            seen.add(j)
            if j not in matches or augment(matches[j], seen):
                matches[j] = i
                return True
        return False
    for i in adjacency: augment(i, set())
    reviewed = [p for p in mode.predictions if p.accepted is not None]
    return dict(marked_moments=len(truth), matched_moments=len(matches), missed_highlights=len(truth)-len(matches),
                recall=len(matches)/len(truth) if truth else None,
                overlap_precision=len(matches)/len(mode.predictions) if mode.predictions else None,
                duplicate_candidates=sum(bool(adjacency[i]) for i in adjacency)-len(matches),
                candidates=len(mode.predictions), reviewed=len(reviewed),
                accepted=sum(p.accepted is True for p in reviewed),
                acceptance_rate=sum(p.accepted is True for p in reviewed)/len(reviewed) if reviewed else None,
                review_seconds=mode.review_seconds)


def compare(data):
    recordings = [Recording.model_validate(item) for item in data['recordings']]
    if not recordings or len({r.recording_id for r in recordings}) != len(recordings):
        raise ValueError('Provide nonempty unique paired recordings')
    return dict(dataset_kind=data.get('dataset_kind', 'unspecified'),
                method='Same ground truth; one-to-one matching with >=50% marked-interval coverage. Human acceptance is separate.',
                recordings=[dict(recording_id=r.recording_id, transcript_only=metrics(r.truth, r.transcript_only),
                                 combined=metrics(r.truth, r.combined)) for r in recordings])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.manifest.read_text()))
    write_json(args.output, result)
    print(json.dumps(result, indent=2))
