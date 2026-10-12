import pytest


def test_legacy_settings_accept_news_defaults_and_article_urls_canonicalize():
    from shorts_local.config import Settings
    from shorts_local.news_models import assert_public_https, canonicalize_article_url

    settings = Settings.model_validate({"topic": "기존 주제"})
    assert settings.production_type == "story"
    assert settings.news_style == "사실만 짧고 크게 전달하고 숫자와 단위는 붙인다"
    assert settings.news_poll_minutes == 60
    assert settings.production_label == "story"
    news = Settings(production_type="news")
    assert news.production_label == "news"
    assert canonicalize_article_url(
        "https://Example.com/story?utm_source=x&fbclid=1#part") == "https://example.com/story"
    for bad in ("http://example.com/a", "https://example.com:444/a",
                "https://127.0.0.1/a", "https://169.254.169.254/a",
                "https://user:secret@example.com/a"):
        with pytest.raises(ValueError) as exc:
            assert_public_https(bad)
        assert "secret" not in str(exc.value)


def test_normalization_is_offline_and_preserves_path_identity(monkeypatch):
    import socket

    from shorts_local.news_models import NewsInput, canonicalize_article_url

    def no_dns(*args, **kwargs):
        pytest.fail("Queue validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert canonicalize_article_url(
        "https://user:secret@Example.com:443/a/?id=7&utm_source=x#foo"
    ) == "https://example.com/a/?id=7"
    data = NewsInput(item_id="a" * 12, url="https://example.com/a")
    assert data.published_at is None
    with pytest.raises(ValueError):
        data.url = "https://example.com/b"


@pytest.mark.parametrize("url", [
    "https://example.com:bad/a", "https://[oops/a", "https://2130706433/a",
    "https://0x7f000001/a", "https://localhost./a", "https://x.local/a",
    "https://instagram.com/p/ABC/", "https://example.com\\@127.0.0.1/a",
])
def test_invalid_article_hosts_are_rejected_offline(url):
    from shorts_local.news_models import canonicalize_article_url

    with pytest.raises(ValueError):
        canonicalize_article_url(url)
