from typing import Literal

from pydantic import Field, model_validator

from .instagram_models import ValueModel
from .models import VideoMetadata


class TranscriptSegment(ValueModel):
    id: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.start >= self.end:
            raise ValueError("발화 종료 시각은 시작 시각보다 뒤여야 합니다.")
        return self


class Transcript(ValueModel):
    language: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self):
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("발화 ID가 중복되었습니다.")
        if any(a.end > b.start for a, b in zip(self.segments, self.segments[1:], strict=False)):
            raise ValueError("발화 시간이 겹치거나 순서가 다릅니다.")
        return self


class Observation(ValueModel):
    id: str = Field(min_length=1)
    time: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=600)


class Analysis(ValueModel):
    transcript: Transcript
    observations: list[Observation] = Field(min_length=1, max_length=48)

    @model_validator(mode="after")
    def unique_observations(self):
        if len({o.id for o in self.observations}) != len(self.observations):
            raise ValueError("관찰 ID가 중복되었습니다.")
        if any(a.time > b.time for a, b in zip(self.observations, self.observations[1:], strict=False)):
            raise ValueError("관찰 시간이 순서대로 나열되지 않았습니다.")
        return self


class EditClip(ValueModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    narration: str | None = Field(default=None, min_length=1, max_length=300)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_range(self):
        if self.start >= self.end:
            raise ValueError("클립의 종료 시각은 시작보다 뒤여야 합니다.")
        return self


class Translation(ValueModel):
    segment_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=300)


class EditPlan(VideoMetadata):
    mode: Literal["narrated", "original"]
    clips: list[EditClip] = Field(min_length=1, max_length=8)
    translations: list[Translation] = Field(default_factory=list)
