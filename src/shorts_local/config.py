import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .process import atomic_json


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)

    topic: str = Field(default="일상 속 작은 반전이 있는 따뜻한 창작 이야기", min_length=2,
                       max_length=500)
    channel_name: str = Field(default="작은 반전", min_length=1, max_length=24)
    planner: Literal["mlx", "codex"] = "mlx"
    text_model: str = "mlx-community/gemma-3-12b-it-4bit"
    speech_model: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
    speech_voice: str = "Sohee"
    visuals: Literal["cards", "mflux"] = "cards"
    image_model: str = "filipstrand/Z-Image-Turbo-mflux-4bit"
    production_type: Literal["story", "instagram", "news"] = "story"
    news_style: str = Field(default="사실만 짧고 크게 전달하고 숫자와 단위는 붙인다",
                            min_length=1, max_length=500)
    news_poll_minutes: int = Field(default=60, ge=15, le=1440)
    instagram_edit_mode: Literal["narrated", "original"] = "narrated"
    instagram_style: str = Field(default="차분하고 자연스러운 한국어로 핵심을 짧게 전달",
                                 min_length=1, max_length=500)
    instagram_accent_color: str = Field(default="#E8B257", pattern=r"^#[0-9A-Fa-f]{6}$")
    instagram_poll_minutes: int = Field(default=60, ge=15, le=1440)
    transcription_model: str = "mlx-community/whisper-large-v3-turbo"
    vision_model: str = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
    width: int = Field(default=1080, ge=360, le=1080, multiple_of=2)
    height: int = Field(default=1920, ge=640, le=1920, multiple_of=2)
    fps: int = Field(default=30, ge=24, le=30)
    min_seconds: float = Field(default=15, ge=1, le=60)
    max_seconds: float = Field(default=90, ge=15, le=180)
    font_path: str | None = None
    schedule_enabled: bool = True
    schedule_time: str = "09:00"
    timezone: str = "Asia/Seoul"
    youtube_enabled: bool = False
    privacy: Literal["private", "unlisted", "public"] = "public"
    made_for_kids: bool = False
    contains_synthetic_media: bool = True
    category_id: str = "24"
    stage_timeout: int = Field(default=1800, ge=30, le=7200)

    @property
    def production_label(self) -> str:
        if self.production_type == "instagram":
            return f"instagram/{self.instagram_edit_mode}"
        return self.production_type

    @field_validator("schedule_time")
    @classmethod
    def check_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("시간은 HH:MM 형식이어야 합니다.")
        return value

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("유효한 IANA 시간대가 필요합니다.") from exc
        return value

    @model_validator(mode="after")
    def check_ranges(self):
        if self.width * 16 != self.height * 9:
            raise ValueError("영상 비율은 9:16이어야 합니다.")
        if self.min_seconds >= self.max_seconds:
            raise ValueError("최소 길이는 최대 길이보다 작아야 합니다.")
        return self

    @classmethod
    def load(cls, path: Path) -> "Settings":
        return cls.model_validate_json(path.read_text()) if path.exists() else cls()

    def save(self, path: Path) -> None:
        atomic_json(path, self.model_dump())


def initialize(root: Path) -> Settings:
    from .store import Store

    return Store(root).profile().settings
