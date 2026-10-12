import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from shorts_local.config import Settings
from shorts_local.store import Store


def test_schema2_upgrade_preserves_instagram_and_history(schema1_home):
    from shorts_local.migrations import _upgrade_instagram
    from tests.test_instagram_migrations import snapshot

    root = schema1_home
    with sqlite3.connect(root / "state.sqlite3") as db:
        _upgrade_instagram(db, root)
        db.execute("UPDATE jobs SET input=?, settings=? WHERE id='legacy-ready'",
                   ('{"shortcode":"Ab","url":"https://instagram.com/p/Ab/"}',
                    Settings(production_type="instagram").model_dump_json()))
    before = snapshot(root)
    old_backup = (root / "backups/pre-instagram.sqlite3").read_bytes()
    for _ in range(2):
        with Store(root).db() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot(root) == before
    assert (root / "backups/pre-instagram.sqlite3").read_bytes() == old_backup
    backup = root / "backups/pre-news.sqlite3"
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (root / "jobs/legacy-ready/video.mp4").read_bytes() == b"unchanged-video"


def test_import_and_feed_compete_for_one_immutable_job(tmp_path):
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    news = NewsStore(s)
    source = news.add_source("https://example.com/feed", now=100)
    first = news.import_article("https://example.com/a?utm_source=x", publish=False)
    original = s.get(first)
    news.apply_feed(source["id"], [{"url": "https://example.com/a", "title": "기사",
                                    "published_at": 200}], now=300)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: NewsStore(Store(tmp_path)).import_article(
            "https://example.com/a?fbclid=2", publish=True), range(4)))
    assert results == [first] * 4
    assert s.get(first) == original
    assert original["input"]["published_at"] is None
    assert original["settings"]["production_type"] == "news"
    assert s.profile().settings.production_type == "story"
    s.add_profile("other", "다른 채널")
    assert news.import_article("https://example.com/a", channel_id="other") != first
    s.update(first, status="failed")
    assert news.enqueue_next() is None
    assert news.materials()[0]["status"] == "reserved"
    with pytest.raises(ValueError):
        s.update(first, input={})


def test_cutoff_reservation_and_rollback(tmp_path):
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    news = NewsStore(s)
    source = news.add_source("https://example.com/rss", now=100)
    assert news.add_source("https://example.com/rss?utm_source=x")["id"] == source["id"]
    entries = [{"url": f"https://example.com/{name}", "published_at": date}
               for name, date in [("old", 99), ("b", 201), ("a", 200)]]
    with pytest.raises(RuntimeError), s.transaction():
        news.apply_feed(source["id"], entries, now=300)
        raise RuntimeError("rollback")
    assert news.materials() == []
    assert news.apply_feed(source["id"], entries, now=300) == 2
    first = news.enqueue_next(key="schedule:2026-10-12")
    assert s.get(first)["input"]["url"] == "https://example.com/a"
    assert news.enqueue_next(key="schedule:2026-10-12") == first
    assert next(i for i in news.materials() if i["canonical_url"].endswith("/b"))["job_id"] is None
    with pytest.raises(ValueError):
        s.enqueue(Settings(production_type="news"))
