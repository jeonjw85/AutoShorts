"""Bounded public HTTPS reads and conservative, dependency-light article extraction."""

import hashlib
import http.client
import io
import json
import re
import socket
import ssl
import sys
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image
from pydantic import Field, model_validator

from .instagram_models import ValueModel
from .news_models import _private_ip, assert_public_https, canonicalize_article_url
from .process import atomic_json


class UnsupportedArticle(ValueError):
    pass


class ArticleDocument(ValueModel):
    title: str = Field(min_length=1, max_length=500)
    site_name: str = Field(min_length=1, max_length=200)
    published_at: float = Field(ge=0, allow_inf_nan=False)
    url: str
    body: str = Field(min_length=400)
    body_sha256: str
    image_url: str | None = Field(default=None, exclude=True)
    image_data: bytes | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def integrity(self):
        if self.body_sha256 != hashlib.sha256(self.body.encode()).hexdigest():
            raise ValueError("기사 본문 해시가 다릅니다.")
        canonicalize_article_url(self.url)
        return self


def parse_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        try:
            date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            date = parsedate_to_datetime(value)
        # A wall-clock time with no zone is ambiguous; a date-only value is UTC.
        if date.tzinfo is None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
                return None
            date = date.replace(tzinfo=UTC)
        return date.timestamp() if date.timestamp() >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class _ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.meta = {}
        self.text = {"article": [], "main": [], "paragraph": [], "title": []}
        self.canonical = None
        self.dates = []
        self.ld_blocks = []
        self._ld_buffer = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._ld_buffer = []
        skipped = tag in {"script", "style", "nav", "aside", "footer", "form", "noscript"}
        if self.stack:
            skipped |= self.stack[-1][1]
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            self.meta[key] = attrs.get("content") or ""
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical = attrs.get("href")
        if tag == "time" and attrs.get("datetime") and not skipped:
            properties = (attrs.get("itemprop") or "").lower().split()
            label = (attrs.get("id") or "") + " " + (attrs.get("class") or "")
            explicit = ("datepublished" in properties or "pubdate" in attrs
                        or re.search(r"published|publication|pubdate|entry-date|post-date", label, re.I))
            article_header = (any(item[2] == "article" for item in self.stack)
                              and self.stack[-1][0] in {"article", "header"})
            modified = "datemodified" in properties or re.search(r"modified|updated", label, re.I)
            if not modified and (explicit or article_header):
                self.dates.append((0 if explicit else 1, attrs["datetime"]))
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}:
            if tag == "br":
                self.handle_data(" ")
            return
        region = tag
        if attrs.get("itemprop") == "articleBody" or re.search(
                r"article[-_]?body|story[-_]?body|news[-_]?body|dic_area",
                (attrs.get("id") or "") + " " + (attrs.get("class") or ""), re.I):
            region = "article"
        self.stack.append((tag, skipped, region))

    def handle_endtag(self, tag):
        if tag == "script" and self._ld_buffer is not None:
            self.ld_blocks.append("".join(self._ld_buffer))
            self._ld_buffer = None
        self.handle_data(" " if tag in {"p", "div", "article", "main", "li"} else "")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self._ld_buffer is not None:
            self._ld_buffer.append(data)
            return
        if not self.stack or self.stack[-1][1]:
            return
        regions = {item[2] for item in self.stack}
        for key in ("article", "main", "title"):
            if key in regions:
                self.text[key].append(data)
        if "p" in regions:
            self.text["paragraph"].append(data)


def _jsonld_objects(blocks: list[str]) -> list[dict]:
    objects = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            objects.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                objects.extend(node for node in graph if isinstance(node, dict))
    return objects


def _jsonld_image(objects: list[dict]) -> str | None:
    for item in objects:
        image = item.get("image")
        for value in image if isinstance(image, list) else [image]:
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("url"), str):
                return value["url"]
    return None


def extract_article(url: str, html: str, *, rss_published_at: float | None = None) -> ArticleDocument:
    page = _ArticleHTML()
    page.feed(html)
    texts = {key: re.sub(r"\s+", " ", "".join(parts)).strip() for key, parts in page.text.items()}
    objects = _jsonld_objects(page.ld_blocks)
    jsonld_body = next((re.sub(r"\s+", " ", item["articleBody"]).strip() for item in objects
                        if isinstance(item.get("articleBody"), str)
                        and len(re.sub(r"\s+", " ", item["articleBody"]).strip()) >= 400), "")
    body = next((text for text in (texts["article"], texts["main"], jsonld_body, texts["paragraph"])
                 if len(text) >= 400), "")
    dates = [page.meta.get(key) for key in ("article:published_time", "datepublished", "parsely-pub-date", "pubdate")]
    dates += [item.get("datePublished") for item in objects
              if isinstance(item.get("datePublished"), str)]
    dates += [date for _, date in sorted(page.dates, key=lambda item: item[0])]
    published = next((parsed for date in dates if (parsed := parse_date(date)) is not None), rss_published_at)
    if len(body) < 400 or published is None:
        raise UnsupportedArticle("기사 본문이 400자 미만이거나 게시 날짜를 확인할 수 없습니다.")
    canonical = canonicalize_article_url(url)
    if page.canonical:
        try:
            candidate = canonicalize_article_url(urljoin(url, page.canonical))
            # Cross-site canonical metadata is not evidence of article identity.
            if urlsplit(candidate).hostname == urlsplit(canonical).hostname:
                canonical = candidate
        except ValueError:
            pass
    headline = next((item["headline"].strip() for item in objects
                     if isinstance(item.get("headline"), str) and item["headline"].strip()), "")
    image = page.meta.get("og:image") or _jsonld_image(objects)
    return ArticleDocument(
        title=(page.meta.get("og:title") or headline or texts["title"] or "기사")[:500],
        site_name=(page.meta.get("og:site_name") or urlsplit(url).hostname)[:200],
        published_at=published, url=canonical, body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        image_url=urljoin(url, image) if image else None)


class _PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, validate all answers, then connect to a numeric sockaddr.
        # TLS still verifies the original hostname. Proxies are never consulted.
        records = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not records or any(_private_ip(row[4][0]) for row in records):
            raise ValueError("공개 HTTPS 주소가 필요합니다.")
        last_error = None
        for family, kind, proto, _, address in records:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                sock.close()
                last_error = exc
        raise OSError("기사 서버에 연결할 수 없습니다.") from last_error


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, connection, response):
        self.connection, self.response = connection, response

    def __iter__(self):
        while chunk := self.response.read(65536):
            yield chunk

    def close(self):
        self.response.close()
        self.connection.close()


class PublicTransport(httpx.BaseTransport):
    def handle_request(self, request):
        assert_public_https(str(request.url))
        timeout = request.extensions.get("timeout", {}).get("read") or 30
        connection = _PublicHTTPS(request.url.host, timeout=timeout, context=ssl.create_default_context())
        try:
            headers = dict(request.headers)
            headers.pop("cookie", None)
            connection.request("GET", request.url.raw_path.decode("ascii"), headers=headers)
            response = connection.getresponse()
            return httpx.Response(response.status, headers=response.getheaders(),
                                  stream=_ResponseStream(connection, response))
        except Exception:
            connection.close()
            raise


def public_client() -> httpx.Client:
    return httpx.Client(transport=PublicTransport(), trust_env=False, timeout=30,
                        headers={"User-Agent": "AutoShorts/0.1", "Accept-Encoding": "identity"})


def read_public(url: str, *, client, limit: int = 5 * 1024**2,
                timeout: float = 30, headers: dict | None = None) -> tuple[bytes, httpx.Response]:
    deadline = time.monotonic() + timeout
    for _ in range(6):
        assert_public_https(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("기사 조회 시간이 초과되었습니다.")
        with client.stream("GET", url, follow_redirects=False, timeout=remaining,
                           headers=headers or {}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("location", ""))
                headers = None
                continue
            if response.status_code == 304:
                return b"", response
            if response.status_code in {401, 403, 404, 410}:
                raise UnsupportedArticle("기사 또는 피드에 접근할 수 없습니다.")
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise UnsupportedArticle("기사 응답이 허용 크기를 초과했습니다.")
                if time.monotonic() > deadline:
                    raise TimeoutError("기사 조회 시간이 초과되었습니다.")
                chunks.append(chunk)
            return b"".join(chunks), response
    raise UnsupportedArticle("리다이렉트 횟수가 너무 많습니다.")


def fetch_article(url: str, *, client=None, rss_published_at: float | None = None,
                   include_image: bool = True) -> ArticleDocument:
    if client is None:
        with public_client() as owned:
            return fetch_article(url, client=owned, rss_published_at=rss_published_at, include_image=include_image)
    data, response = read_public(canonicalize_article_url(url), client=client)
    encoding = response.encoding or "utf-8"
    doc = extract_article(str(response.url), data.decode(encoding, errors="replace"),
                          rss_published_at=rss_published_at)
    if include_image:
        _attach_image(doc, client)
    return doc


def _attach_image(doc, client):
    if doc.image_url:
        try:
            data, _ = read_public(doc.image_url, client=client, limit=12 * 1024**2)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 24_000_000:
                    raise ValueError("대표 이미지 크기 초과")
                image = image.convert("RGB")
                image.thumbnail((1920, 1920))
                output = io.BytesIO()
                image.save(output, "JPEG", quality=90)
                doc.image_data = output.getvalue()
        except (ValueError, OSError, httpx.HTTPError, Image.DecompressionBombError):
            doc.image_url = None


def main():
    folder = Path(sys.argv[1])
    request = json.loads((folder / "request.json").read_text())
    try:
        with public_client() as client:
            doc = fetch_article(request["url"], client=client, rss_published_at=request["published_at"],
                                include_image=False)
            # Preserve a usable text article even if the optional image hits the deadline.
            atomic_json(folder / "article.json", doc.model_dump())
            _attach_image(doc, client)
            if doc.image_data:
                (folder / "image.jpg").write_bytes(doc.image_data)
    except ValueError:
        atomic_json(folder / "error.json", {"unsupported": True})
    except Exception:
        atomic_json(folder / "error.json", {"unsupported": False})


if __name__ == "__main__":
    main()
