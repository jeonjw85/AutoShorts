import hashlib
import socket

import httpx
import pytest

from tests.news_helpers import BODY, DATE, html


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


def test_article_main_text_publication_priority_and_hash():
    from shorts_local.news_fetch import extract_article

    doc = extract_article("https://example.com/a", html(), rss_published_at=100)
    assert doc.published_at == DATE
    assert doc.site_name == "서울뉴스"
    assert "제외할 메뉴" not in doc.body
    assert doc.body_sha256 == hashlib.sha256(doc.body.encode()).hexdigest()
    assert extract_article("https://example.com/a", html(date=""), rss_published_at=100).published_at == 100


@pytest.mark.parametrize("body,date", [("가" * 399, "2026-10-11T23:00:00Z"), (BODY, "")])
def test_unsupported_article_has_no_invented_date(body, date):
    from shorts_local.news_fetch import UnsupportedArticle, extract_article

    with pytest.raises(UnsupportedArticle):
        extract_article("https://example.com/a", html(body, date), rss_published_at=None)


@pytest.mark.parametrize("target", ["https://127.0.0.1/a", "http://example.com/a",
                                    "https://private.example.com/a"])
def test_redirect_is_checked_before_second_connection(target, monkeypatch):
    from shorts_local.news_fetch import fetch_article

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "",
         ("10.0.0.1" if host == "private.example.com" else "93.184.216.34", 443))])
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(ValueError):
        fetch_article("https://example.com/a", client=client)
    assert requests == ["https://example.com/a"]


def test_broken_representative_image_keeps_text_article():
    from shorts_local.news_fetch import fetch_article

    def respond(request):
        if request.url.path == "/a":
            return httpx.Response(200, text=html(image="https://example.com/photo?signature=secret"))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        doc = fetch_article("https://example.com/a", client=client)
    assert doc.image_data is None
    assert "signature" not in doc.model_dump_json()
    assert doc.body == BODY.strip()


def test_oversized_article_is_rejected():
    from shorts_local.news_fetch import UnsupportedArticle, fetch_article

    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"x" * (5 * 1024**2 + 1)))) as client:
        with pytest.raises(UnsupportedArticle):
            fetch_article("https://example.com/a", client=client)


@pytest.mark.parametrize("address", ["100.100.100.200", "100.64.0.1", "::ffff:100.100.100.200"])
@pytest.mark.parametrize("boundary", ["url", "socket"])
def test_shared_metadata_addresses_are_blocked_before_socket(monkeypatch, address, boundary):
    from shorts_local.news_fetch import _PublicHTTPS
    from shorts_local.news_models import assert_public_https

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Unsafe socket must never be created"))
    with pytest.raises(ValueError):
        if boundary == "url":
            assert_public_https("https://example.com/a")
        else:
            _PublicHTTPS("example.com", timeout=1).connect()


def test_publication_time_ignores_site_clock_and_modified_date():
    from shorts_local.news_fetch import extract_article, parse_date

    page = ('<header><time datetime="2026-10-12T12:00:00Z">현재 시각</time></header>'
            '<article><header><time itemprop="dateModified" datetime="2026-10-11T00:00:00Z">수정</time>'
            '<time datetime="2026-10-10T00:00:00Z">게시</time></header>'
            f'<p>{BODY}</p></article>')
    doc = extract_article("https://example.com/a", page, rss_published_at=100)
    assert doc.published_at == parse_date("2026-10-10T00:00:00Z")


def test_unrelated_time_uses_rss_or_rejects_missing_publication():
    from shorts_local.news_fetch import UnsupportedArticle, extract_article

    page = ('<header><time datetime="2026-10-12T12:00:00Z">현재 시각</time></header>'
            '<meta name="date" content="2026-10-12T12:00:00Z">'
            f'<article><p>{BODY}</p><aside><time datetime="2026-10-09T00:00:00Z">관련 기사</time></aside>'
            '<p>행사 날짜 <time datetime="2026-10-15T00:00:00Z">다음 주</time></p></article>')
    assert extract_article("https://example.com/a", page, rss_published_at=100).published_at == 100
    with pytest.raises(UnsupportedArticle):
        extract_article("https://example.com/a", page)
