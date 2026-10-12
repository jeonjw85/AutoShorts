import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ConfigDict, Field, field_validator

from .instagram_models import LocalID, ValueModel

_MESSAGE = "공개 HTTPS 기사 주소가 필요합니다."
_TRACKING = {"fbclid", "igsh", "igshid"}


def _private_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not address.is_global or address.is_multicast or address.is_reserved


def _parse_url(value: str):
    try:
        url = urlsplit(value.strip())
        host = (url.hostname or "").encode("idna").decode().lower().rstrip(".")
        if (any(c.isspace() or ord(c) < 32 for c in value.strip()) or "\\" in value
                or url.scheme != "https" or url.port not in (None, 443)
                or len(host) > 253 or "." not in host
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,63}", host)
                or any(part.startswith("-") or part.endswith("-") or len(part) > 63
                       for part in host.split("."))
                or host.endswith((".localhost", ".local", ".internal"))
                or host == "instagram.com" or host.endswith(".instagram.com")):
            raise ValueError
        return url, host
    except (ValueError, UnicodeError):
        raise ValueError(_MESSAGE) from None


def assert_public_https(value: str) -> None:
    url, host = _parse_url(value)
    if url.username is not None or url.password is not None:
        raise ValueError(_MESSAGE)
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        # Resolver outages are network failures, not evidence of unsafe input.
        raise OSError("기사 호스트를 조회하지 못했습니다.") from None
    if not records or any(_private_ip(item[4][0]) for item in records):
        raise ValueError(_MESSAGE)


def canonicalize_article_url(value: str) -> str:
    url, host = _parse_url(value)
    path = url.path or "/"
    query = urlencode([(key, item) for key, item in parse_qsl(url.query, keep_blank_values=True)
                       if key.lower() not in _TRACKING and not key.lower().startswith("utm_")])
    return urlunsplit(("https", host, path, query, ""))


class NewsInput(ValueModel):
    model_config = ConfigDict(frozen=True)
    item_id: LocalID
    source_id: LocalID | None = None
    url: str
    published_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def canonical(cls, value):
        return canonicalize_article_url(value)
