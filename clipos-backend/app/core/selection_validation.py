"""Strict model-output contracts. Validation runs inside provider fallback."""
import json
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator


def parse_json(raw):
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    return json.loads(cleaned, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    clip_id: str | None = Field(default=None, max_length=128)
    start: float = Field(ge=0, strict=True)
    end: float = Field(gt=0, strict=True)
    hook_text: str = Field(min_length=1, max_length=240)
    virality_score: float = Field(ge=0, le=1, strict=True)
    reasoning: str = Field(min_length=1, max_length=600)

    @model_validator(mode='after')
    def interval(self):
        if self.end <= self.start or not self.hook_text.strip() or not self.reasoning.strip():
            raise ValueError('Invalid candidate interval or empty description')
        return self


def candidates_response(raw, start=0, end=float('inf'), minimum=0, maximum=float('inf'), limit=30):
    data = parse_json(raw)
    if not isinstance(data, list) or len(data) > limit:
        raise ValueError('Expected bounded candidate array')
    result = [Candidate.model_validate(h).model_dump() for h in data]
    ids = [h['clip_id'] for h in result if h['clip_id'] is not None]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate candidate IDs')
    for h in result:
        if not start <= h['start'] < h['end'] <= end or not minimum <= h['end']-h['start'] <= maximum:
            raise ValueError('Candidate outside source/duration bounds')
    return result


class Score(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    clip_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=60)
    final_hook_text: str = Field(min_length=1, max_length=240)
    final_score: float = Field(ge=0, le=1, strict=True)
    rank_reasoning: str = Field(min_length=1, max_length=600)

    @model_validator(mode='after')
    def nonempty(self):
        if any(not text.strip() for text in (self.clip_id, self.title, self.final_hook_text, self.rank_reasoning)):
            raise ValueError('Scoring text must not be blank')
        return self


def scores_response(raw, ids):
    data = parse_json(raw)
    if not isinstance(data, list) or len(data) != len(ids):
        raise ValueError('Scoring must return every requested clip once')
    result = [Score.model_validate(item).model_dump() for item in data]
    actual = [item['clip_id'] for item in result]
    if len(set(actual)) != len(actual) or set(actual) != set(ids):
        raise ValueError('Unknown, missing or duplicate scored clip ID')
    return result
