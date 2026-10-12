import json
import socket
from threading import Event

import pytest
from typer.testing import CliRunner

from shorts_local.cli import app
from shorts_local.news_store import NewsStore
from shorts_local.store import Store
from shorts_local.telegram import handle_update
from tests.test_service import update


def test_news_cli_defaults_conflicts_and_sources(tmp_path, story_data, monkeypatch):
    import shorts_local.news_collector as collector

    runner, home = CliRunner(), ["--home", str(tmp_path)]
    result = runner.invoke(app, home + ["import-article", "https://example.com/a", "--local-only"])
    assert result.exit_code == 0, result.output
    s = Store(tmp_path)
    assert s.list()[0]["settings"]["production_type"] == "news"
    assert not s.list()[0]["publish"]
    assert s.profile().settings.production_type == "story"
    document = tmp_path / "story.json"
    document.write_text(json.dumps(story_data))
    for flags in (["--story", str(document)], ["--topic", "기사"], ["--mode", "original"]):
        assert runner.invoke(app, home + ["create", "--type", "news", *flags]).exit_code != 0
    assert len(s.list()) == 1
    empty = runner.invoke(app, home + ["create", "--type", "news"])
    assert empty.exit_code == 0 and "소재 대기" in empty.output
    assert runner.invoke(app, home + ["news-source", "add", "https://example.com/rss"]).exit_code == 0
    source = NewsStore(s).sources()[0]
    assert source["id"] in runner.invoke(app, home + ["news-source", "list"]).output
    for command, active in (("pause", False), ("resume", True)):
        assert runner.invoke(app, home + ["news-source", command, source["id"]]).exit_code == 0
        assert bool(NewsStore(s).source(source["id"])["active"]) == active
    calls = []

    def collect(root, **kwargs):
        with Store(root).transaction():
            calls.append(kwargs["channel_id"])

    monkeypatch.setattr(collector, "run_news_collection", collect)
    assert runner.invoke(app, home + ["news-source", "scan"]).exit_code == 0
    assert calls == ["default"]


def test_telegram_article_is_atomic_offline_and_replay_safe(tmp_path, monkeypatch):
    s = Store(tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS inside Telegram"))
    ack = s.accept_telegram
    monkeypatch.setattr(s, "accept_telegram", lambda *a: (_ for _ in ()).throw(RuntimeError("ack fails")))
    with pytest.raises(RuntimeError):
        handle_update(s, update("https://example.com/a", update_id=100), 42)
    assert s.list() == [] and NewsStore(s).materials() == []
    monkeypatch.setattr(s, "accept_telegram", ack)
    reply = handle_update(s, update("https://example.com/a", update_id=100), 42)
    assert "news 작업 등록" in reply["text"]
    s.add_profile("other", "다른 채널")
    s.set_value("telegram_channel", "other")
    assert handle_update(s, update("https://example.com/a", update_id=100), 42) == reply
    assert len(s.list()) == 1
    handle_update(s, update("https://instagram.com/p/Ab/", update_id=101), 42)
    assert s.list()[0]["settings"]["production_type"] == "instagram"
    for text in ("/news_add https://example.com/feed", "/news_add https://example.com/feed"):
        handle_update(s, update(text, update_id=s.value("telegram_offset")), 42)
    assert len(NewsStore(s).sources("other")) == 1
    handle_update(s, update("/news_scan", update_id=110), 42)
    assert NewsStore(s).sources("other")[0]["next_run"] == 0
    story = s.enqueue(channel_id="other")
    handle_update(s, update("/set type news", update_id=111), 42)
    assert s.get(story)["settings"]["production_type"] == "story"
    before = len(s.list())
    assert "소재 대기" in handle_update(s, update("/create", update_id=112), 42)["text"]
    assert len(s.list()) == before


def test_serve_processes_pause_while_news_feed_is_slow(tmp_path, monkeypatch):
    from shorts_local import news_collector, service

    s = Store(tmp_path)
    s.update_profile_settings("default", {"schedule_enabled": False})
    NewsStore(s).add_source("https://example.com/feed")
    started, release, stop = Event(), Event(), Event()

    def fetch(source, folder):
        started.set()
        assert release.wait(5), "Telegram was blocked by RSS collection"
        return {"entries": []}

    class Bot:
        user_id = 42

        def poll(self, offset):
            assert started.wait(3)
            return [update("/pause", update_id=100)]

        def send(self, reply):
            assert Store(tmp_path).profile().paused
            assert not release.is_set()
            release.set()
            stop.set()

        def close(self):
            pass

    monkeypatch.setattr(news_collector, "_fetch_feed", fetch)
    monkeypatch.setattr(service, "credentials", lambda root: ("fixture", 42))
    monkeypatch.setattr(service, "Telegram", lambda *a: Bot())
    service.serve(tmp_path, stop)
    assert release.is_set() and s.profile().paused
