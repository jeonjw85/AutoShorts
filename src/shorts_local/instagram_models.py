"""Dependency-light values shared by the queue, collector, and media workers."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LocalID = Annotated[str, Field(pattern=r"^[0-9a-f]{12}$")]
AccountID = Annotated[str, Field(pattern=r"^[0-9]+$")]
ErrorCode = Literal["busy", "deferred", "rate_limit", "auth_wait", "unavailable",
                    "unsupported", "transient"]


def parse_instagram_url(value: str) -> tuple[str, str]:
    message = "인스타그램 릴스 또는 단일 게시물 HTTPS 링크가 필요합니다."
    value = value.strip()
    try:
        url = urlsplit(value)
        match = re.fullmatch(r"/(?:reel|p)/([A-Za-z0-9_-]+)/?", url.path)
        if (any(c.isspace() or ord(c) < 32 for c in value) or url.scheme != "https"
                or url.hostname not in {"instagram.com", "www.instagram.com"}
                or url.username is not None or url.password is not None
                or url.port not in (None, 443) or not match):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    shortcode = match[1]
    return shortcode, f"https://www.instagram.com/reel/{shortcode}/"


def normalize_username(value: str) -> str:
    name = value.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,30}", name):
        raise ValueError("인스타그램 계정명은 영문·숫자·밑줄·마침표 1~30자입니다.")
    return name


class ValueModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class PostReference(ValueModel):
    shortcode: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    url: str

    @model_validator(mode="after")
    def canonical_reference(self):
        shortcode, url = parse_instagram_url(self.url)
        if shortcode != self.shortcode:
            raise ValueError("원본 식별자와 링크가 다릅니다.")
        object.__setattr__(self, "url", url)
        return self


class InstagramInput(PostReference):
    model_config = ConfigDict(frozen=True)
    item_id: LocalID
    source_id: LocalID | None = None


class InstagramPost(PostReference):
    author: str
    owner_id: AccountID
    published_at: float = Field(ge=0)
    caption: str
    media_type: Literal["video", "photo", "carousel", "unsupported"]

    @field_validator("author")
    @classmethod
    def username(cls, value):
        return normalize_username(value)


class SourceMedia(ValueModel):
    post: InstagramPost
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=300 * 1024**2)
    duration: float = Field(ge=1, le=600)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    has_audio: bool


class ScanBatch(ValueModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_id: LocalID
    base_revision: int = Field(ge=0)
    started_at: float = Field(ge=0)
    account_id: AccountID | None = None
    username: str | None = None
    posts: list[InstagramPost] = Field(default_factory=list, max_length=200)
    cursor: dict | None = None
    complete: bool = False
    error_code: ErrorCode | None = None
    retry_at: float | None = Field(default=None, ge=0)

    @field_validator("username")
    @classmethod
    def optional_username(cls, value):
        return normalize_username(value) if value is not None else None
