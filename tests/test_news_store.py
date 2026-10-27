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
    s.update_profile_settings("default", {"news_max_age_hours": 0})
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


def test_feed_skips_articles_older_than_the_freshness_window(tmp_path):
    from shorts_local.news_store import NewsStore
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "news"})
    news = NewsStore(store)
    source = news.add_source("https://example.com/feed", now=0)
    now = 100 * 3600
    added = news.apply_feed(source["id"], [
        {"url": "https://example.com/old", "published_at": now - 49 * 3600},
        {"url": "https://example.com/new", "published_at": now - 47 * 3600},
    ], now=now)
    assert added == 1
    assert [item["canonical_url"] for item in news.materials()] == ["https://example.com/new"]


def test_queue_selection_respects_the_current_freshness_window(tmp_path):
    from shorts_local.news_store import NewsStore
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "news"})
    news = NewsStore(store)
    source = news.add_source("https://example.com/feed", now=0)
    now = 100 * 3600
    news.apply_feed(source["id"], [{"url": "https://example.com/a", "published_at": now - 2 * 3600}],
                    now=now)
    store.update_profile_settings("default", {"news_max_age_hours": 1})
    assert news.enqueue_next() is None
    assert news.channel_state("default")["available"] == 0


def test_replace_failed_enqueues_at_most_three_replacements(tmp_path):
    from shorts_local.news_store import NewsStore
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "news"})
    news = NewsStore(store)
    source = news.add_source("https://example.com/feed", now=0)
    news.apply_feed(source["id"], [
        {"url": f"https://example.com/{index}", "published_at": 100 + index} for index in range(6)
    ], now=1000)
    first = news.enqueue_next(key="schedule:2026-10-27", now=1000)
    news.reject_item(store.get(first)["input"]["item_id"], "기획 검증 실패")
    replacements = [news.replace_failed("default", schedule_key="schedule:2026-10-27", now=1000)
                    for _ in range(4)]
    assert all(replacements[:3]) and replacements[3] is None
    assert store.value("news_replace:default:2026-10-27") == 3
    assert {store.get(job)["dedup_key"] for job in replacements[:3]} == {
        "schedule:2026-10-27:r1", "schedule:2026-10-27:r2", "schedule:2026-10-27:r3"}


def test_scheduled_failure_blocks_the_job_and_keeps_manual_jobs_isolated(tmp_path):
    from shorts_local.news_plan import NewsPlanError
    from shorts_local.news_store import NewsStore
    from shorts_local.pipeline import handle_news_plan_failure
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "news"})
    news = NewsStore(store)
    source = news.add_source("https://example.com/feed", now=0)
    news.apply_feed(source["id"], [
        {"url": f"https://example.com/{index}", "published_at": 100 + index} for index in range(3)
    ], now=1000)
    scheduled = news.enqueue_next(key="schedule:2026-10-27", now=1000)
    manual = news.enqueue_next(key="telegram:1", now=1000)
    replacement = handle_news_plan_failure(store, store.get(scheduled), NewsPlanError("검증 실패"),
                                           now=1000)
    assert store.get(scheduled)["status"] == "blocked"
    assert store.get(replacement)["dedup_key"] == "schedule:2026-10-27:r1"
    assert handle_news_plan_failure(store, store.get(manual), NewsPlanError("검증 실패"),
                                    now=1000) is None
    assert store.get(manual)["status"] == "blocked"


def test_replace_failed_counts_chained_schedule_keys_against_the_same_day(tmp_path):
    from shorts_local.news_store import NewsStore
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "news"})
    news = NewsStore(store)
    source = news.add_source("https://example.com/feed", now=0)
    news.apply_feed(source["id"], [
        {"url": f"https://example.com/{index}", "published_at": 100 + index} for index in range(6)
    ], now=1000)
    news.enqueue_next(key="schedule:2026-10-27", now=1000)
    key, keys = "schedule:2026-10-27", []
    for _ in range(4):
        replacement = news.replace_failed("default", schedule_key=key, now=1000)
        if replacement is None:
            break
        job = store.get(replacement)
        keys.append(job["dedup_key"])
        news.reject_item(job["input"]["item_id"], "기획 검증 실패")
        key = job["dedup_key"]
    assert keys == ["schedule:2026-10-27:r1", "schedule:2026-10-27:r2", "schedule:2026-10-27:r3"]
    assert store.value("news_replace:default:2026-10-27") == 3
