"""RSS 2.0 / Atom parsing; invoked in a bounded collector subprocess."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .news_fetch import parse_date, public_client, read_public
from .news_models import canonicalize_article_url
from .process import atomic_json


def parse_feed(xml: str | bytes, *, registered_at: float) -> list[dict]:
    raw = xml.encode() if isinstance(xml, str) else xml
    if len(raw) > 5 * 1024**2 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("지원하지 않는 피드 형식입니다.")
    root = ET.fromstring(raw)
    atom = "{http://www.w3.org/2005/Atom}"
    if root.tag == f"{atom}feed":
        entries, prefix = root.findall(f"{atom}entry"), atom
    elif root.tag == "rss":
        entries, prefix = root.findall("./channel/item"), ""
    else:
        raise ValueError("RSS 2.0 또는 Atom 피드가 필요합니다.")
    result = []
    for item in entries[:200]:
        if prefix:
            link = next((node.get("href") for node in item.findall(f"{atom}link")
                         if node.get("rel", "alternate") == "alternate"), None)
            date = item.findtext(f"{atom}published") or item.findtext(f"{atom}updated")
        else:
            link, date = item.findtext("link"), item.findtext("pubDate")
        published = parse_date(date)
        if not link or published is None or published < registered_at:
            continue
        try:
            url = canonicalize_article_url(link)
        except ValueError:
            continue
        result.append({"url": url, "title": (item.findtext(f"{prefix}title") or "")[:500],
                       "published_at": published})
    return result


def fetch_feed(source: dict) -> dict:
    headers = {}
    for field, name in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
        if source.get(field):
            headers[name] = source[field]
    with public_client() as client:
        raw, response = read_public(source["feed_url"], client=client, timeout=110, headers=headers)
    result = {"entries": [] if response.status_code == 304 else
              parse_feed(raw, registered_at=source["registered_at"])}
    for field, name in (("etag", "etag"), ("last_modified", "last-modified")):
        value = response.headers.get(name, source.get(field) if response.status_code == 304 else None)
        result[field] = value[:1000] if value else None
    return result


def main():
    folder = Path(sys.argv[1])
    source = json.loads((folder / "request.json").read_text())
    try:
        result = fetch_feed(source)
    except Exception:
        result = {"error": "피드 조회 실패"}
    atomic_json(folder / "result.json", result)


if __name__ == "__main__":
    main()
