import re

from pydantic import BaseModel, ConfigDict, field_validator

from .config import Settings


def validate_channel_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        raise ValueError("채널 ID는 영문 소문자로 시작하는 1~32자의 소문자·숫자·_·-입니다.")
    return value


def validate_youtube_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
        raise ValueError("올바르지 않은 YouTube 채널 ID입니다.")
    return value


class ChannelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    settings: Settings
    paused: bool = False
    youtube_channel_id: str | None = None
    youtube_title: str | None = None

    @field_validator("id")
    @classmethod
    def check_id(cls, value):
        return validate_channel_id(value)

    @property
    def label(self) -> str:
        return f"[{self.settings.channel_name} · {self.id}]"


class UploadTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str
    youtube_id: str

    @field_validator("channel_id")
    @classmethod
    def check_channel(cls, value):
        return validate_channel_id(value)

    @field_validator("youtube_id")
    @classmethod
    def check_youtube(cls, value):
        return validate_youtube_id(value)

    def artifact_fields(self) -> dict:
        return {"channel_id": self.channel_id, "youtube_channel_id": self.youtube_id}
