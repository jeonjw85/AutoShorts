import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    heading: str = Field(min_length=1, max_length=30)
    narration: str = Field(min_length=10, max_length=110)
    image_prompt: str = Field(min_length=10, max_length=600)

    @field_validator("narration")
    @classmethod
    def korean_narration(cls, value):
        if len(re.findall(r"[가-힣]", value)) < 5:
            raise ValueError("내레이션은 한국어 문장이어야 합니다.")
        return value

    @field_validator("image_prompt")
    @classmethod
    def english_prompt(cls, value):
        if re.search(r"[가-힣]", value) or len(re.findall(r"[A-Za-z]", value)) < 10:
            raise ValueError("이미지 프롬프트는 영어로 작성해야 합니다.")
        return value


class VideoMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=2, max_length=70)
    description: str = Field(min_length=5, max_length=2000)

    @field_validator("title", "description")
    @classmethod
    def clean_metadata(cls, value):
        if "<" in value or ">" in value:
            raise ValueError("YouTube 메타데이터에 꺾쇠는 사용할 수 없습니다.")
        return value


class Story(VideoMetadata):
    scenes: list[Scene] = Field(min_length=3, max_length=8)

    @field_validator("scenes")
    @classmethod
    def distinct_scenes(cls, value):
        if len({scene.narration for scene in value}) != len(value):
            raise ValueError("동일한 내레이션이 반복됩니다.")
        return value
