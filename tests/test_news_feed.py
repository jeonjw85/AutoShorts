from datetime import datetime
from zoneinfo import ZoneInfo

from shorts_local.service import schedule_due
from shorts_local.store import Store


def test_atom_cutoff_missing_links_and_item_limit():
    from shorts_local.news_feed import parse_feed

    xml = '<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(
        f'<entry><title>기사 {i}</title><published>2026-10-12T00:00:00Z</published>'
        f'<link href="https://example.com/{i}"/></entry>' for i in range(205)) + '</feed>'
    assert len(parse_feed(xml, registered_at=0)) == 200
    assert parse_feed(xml, registered_at=2_000_000_000) == []
    assert parse_feed('<rss><channel><item><title>no link</title></item></channel></rss>',
                      registered_at=0) == []


def test_news_schedule_waits_and_survives_type_change(tmp_path):
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    s.update_profile_settings("default", {"production_type": "news", "schedule_time": "00:00"})
    news = NewsStore(s)
    source = news.add_source("https://example.com/feed", now=100)
    now = datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Seoul"))
    assert schedule_due(s, now=now) is None
    assert s.list() == []
    news.apply_feed(source["id"], [{"url": "https://example.com/b", "published_at": 201},
                                    {"url": "https://example.com/a", "published_at": 200}], now=300)
    first = schedule_due(s, now=now)
    assert s.get(first)["input"]["url"].endswith("/a")
    s.update_profile_settings("default", {"production_type": "story"})
    assert schedule_due(s, now=now) == first
    s.update_profile_settings("default", {"production_type": "news"})
    second = schedule_due(s, now=now.replace(day=13, hour=0))
    assert second != first
    assert s.get(second)["dedup_key"] == "schedule:2026-10-13"


def test_collection_failure_backoff_pause_and_channel_isolation(tmp_path, monkeypatch):
    from shorts_local import news_collector
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    news = NewsStore(s)
    source = news.add_source("https://example.com/feed", now=100)
    s.add_profile("other", "다른 채널")
    other = news.add_source("https://example.com/other", channel_id="other", now=101)

    def fetch(source, folder):
        if source["channel_id"] == "default":
            raise TimeoutError
        return {"entries": [{"url": "https://example.com/a", "published_at": 200}],
                "etag": '"v1"', "last_modified": None}

    monkeypatch.setattr(news_collector, "_fetch_feed", fetch)
    assert news_collector.run_news_collection(tmp_path, now=300) == source["id"]
    assert news.source(source["id"])["next_run"] == 600
    assert news_collector.run_news_collection(tmp_path, now=301) == other["id"]
    assert len(news.materials("other")) == 1
    news_collector.run_news_collection(tmp_path, channel_id="default", now=600)
    assert news.source(source["id"])["next_run"] == 1200
    assert len(s.pending_notifications()) == 1
    news.set_source_active(source["id"], False)
    assert news.due_source(channel_id="default", now=2000) is None
    news.set_source_active(source["id"], True)
    s.set_paused(True, channel_id="default")
    assert news.due_source(channel_id="default", now=2000) is None
    other_job = s.enqueue(channel_id="other")
    assert s.claim()["id"] == other_job
    s.set_paused(True)
    assert news.due_source(now=5000) is None


def test_entries_published_during_request_are_kept_with_validator(tmp_path, monkeypatch):
    from shorts_local import news_collector
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    news = NewsStore(s)
    source = news.add_source("https://example.com/feed", now=900)
    clock = [1000]
    monkeypatch.setattr(news_collector.time, "time", lambda: clock[0])

    def fetch(source, folder):
        clock[0] = 1002
        return {"entries": [{"url": "https://example.com/a", "published_at": 1001}], "etag": '"v1"'}

    monkeypatch.setattr(news_collector, "_fetch_feed", fetch)
    news_collector.run_news_collection(tmp_path)
    assert [item["canonical_url"] for item in news.materials()] == ["https://example.com/a"]
    assert news.source(source["id"])["last_success"] == 1002
    assert news.source(source["id"])["etag"] == '"v1"'


def test_future_entries_do_not_get_stuck_behind_cache_validators(tmp_path):
    from shorts_local.news_store import NewsStore

    news = NewsStore(Store(tmp_path))
    source = news.add_source("https://example.com/feed", now=900)
    result = {"entries": [{"url": "https://example.com/a", "published_at": 1100}],
              "etag": '"v1"', "last_modified": "Wed, 01 Oct 2026 00:00:00 GMT"}
    news.record_success(source["id"], result, now=1000)
    assert news.materials() == []
    assert news.source(source["id"])["etag"] is None
    assert news.source(source["id"])["last_modified"] is None
    news.record_success(source["id"], result, now=1101)
    assert len(news.materials()) == 1
    assert news.source(source["id"])["etag"] == '"v1"'


def test_low_disk_defers_feed_download_without_consuming_source(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace

    from shorts_local import news_collector
    from shorts_local.news_store import NewsStore

    news = NewsStore(Store(tmp_path))
    source = news.add_source("https://example.com/feed", now=100)
    requests = []

    def fetch(source, folder):
        requests.append(source["id"])
        return {"entries": []}

    monkeypatch.setattr(news_collector, "_fetch_feed", fetch)
    monkeypatch.setattr(shutil, "disk_usage", lambda root: SimpleNamespace(free=1))
    assert news_collector.run_news_collection(tmp_path, now=300) is None
    assert requests == []
    assert news.source(source["id"]) == source
    monkeypatch.setattr(shutil, "disk_usage", lambda root: SimpleNamespace(free=5 * 1024**3))
    assert news_collector.run_news_collection(tmp_path, now=300) == source["id"]
    assert requests == [source["id"]]
