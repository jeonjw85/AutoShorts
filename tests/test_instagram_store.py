from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shorts_local.config import Settings
from shorts_local.instagram_models import InstagramInput
from shorts_local.service import schedule_due
from shorts_local.store import Store
from tests.instagram_helpers import batch, media, post


def collection(root):
    from shorts_local.instagram_store import InstagramStore

    store = Store(root)
    return store, InstagramStore(store)


def test_import_scan_and_competing_reservations_share_one_job(tmp_path):
    s, ig = collection(tmp_path)
    source = ig.add_source("@pets", now=100)
    first = ig.import_video("https://instagram.com/p/Ab/", mode="original", publish=False)
    original = s.get(first)
    ig.apply_scan(batch(source, [post()]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: collection(tmp_path)[1].import_video(
            "https://instagram.com/reel/Ab/?tracking=1", mode="narrated", publish=True), range(8)))
    assert ids == [first] * 8
    assert s.get(first) == original
    assert len(ig.materials()) == len(s.list()) == 1
    s.add_profile("animals", "동물")
    assert ig.import_video("https://instagram.com/p/Ab/", channel_id="animals") != first
    assert s.get(first)["input"]["source_id"] is None


def test_existing_schedule_does_not_consume_next_item(tmp_path):
    s, ig = collection(tmp_path)
    s.update_profile_settings("default", {"production_type": "instagram"})
    source = ig.add_source("pets", now=100)
    ig.apply_scan(batch(source, [post("A"), post("B", 201)]))
    now = datetime(2026, 10, 10, 9, tzinfo=ZoneInfo("Asia/Seoul"))
    first = schedule_due(s, now=now)
    assert first
    assert [schedule_due(s, now=now) for _ in range(3)] == [first] * 3
    remaining = next(item for item in ig.materials() if item["shortcode"] == "B")
    assert remaining["status"] == "available" and remaining["job_id"] is None


def test_empty_day_late_material_and_type_change(tmp_path):
    s, ig = collection(tmp_path)
    s.update_profile_settings("default", {"production_type": "instagram"})
    source = ig.add_source("pets", now=100)
    now = datetime(2026, 10, 10, 11, tzinfo=ZoneInfo("Asia/Seoul"))
    assert schedule_due(s, now=now) is None
    assert s.list() == []
    ig.apply_scan(batch(source, [post("A"), post("B", 201)]))
    first = schedule_due(s, now=now)
    s.update_profile_settings("default", {"production_type": "story", "schedule_time": "10:00"})
    assert schedule_due(s, now=now) == first
    s.update_profile_settings("default", {"production_type": "instagram"})
    second = schedule_due(s, now=now.replace(day=12))
    assert s.get(second)["dedup_key"] == "schedule:2026-10-12"
    assert len(s.list()) == 2


def test_registered_cutoff_order_pause_and_failure(tmp_path):
    s, ig = collection(tmp_path)
    source = ig.add_source("pets", now=100)
    ig.apply_scan(batch(source, [post("old", 99), post("B", 201), post("A", 201),
                                 post("photo", 202, media_type="photo")]))
    assert {x["shortcode"] for x in ig.materials()} == {"A", "B", "photo"}
    ig.set_source_active(source["id"], False)
    assert ig.enqueue_next() is None
    ig.set_source_active(source["id"], True)
    s.set_paused(True)
    first = ig.enqueue_next()
    assert s.get(first)["input"]["shortcode"] == "A"
    assert s.claim() is None
    s.update(first, status="failed")
    second = ig.enqueue_next()
    assert s.get(second)["input"]["shortcode"] == "B"
    s.set_paused(False)
    s.set_paused(True, channel_id="default")
    assert s.claim() is None
    s.set_paused(False, channel_id="default")
    assert s.claim()["id"] == second
    assert ig.enqueue_next() is None


def test_scan_revision_and_cross_channel_provenance(tmp_path):
    s, ig = collection(tmp_path)
    source = ig.add_source("pets", now=100)
    value = batch(source, [post()])
    assert ig.apply_scan(value)
    assert not ig.apply_scan(value)
    assert ig.source(source["id"])["scan_revision"] == 1
    item = ig.materials()[0]
    s.add_profile("other", "다른 채널")
    settings = Settings(production_type="instagram")
    input = InstagramInput(item_id=item["id"], source_id=source["id"],
                           shortcode="Ab", url=item["url"])
    with pytest.raises(ValueError):
        s.enqueue(settings, input=input, channel_id="other")
    first = s.enqueue(settings, input=input)
    assert s.enqueue(settings, input=input) == first
    with pytest.raises(ValueError):
        ig.record_media(item["id"], media("Other"), job_id=first)
    ig.record_media(item["id"], media(), job_id=first)
    ig.reject_item(item["id"], "unavailable")
    assert ig.materials()[0]["job_id"] == first
    assert ig.materials()[0]["status"] == "unavailable"
    with pytest.raises(ValueError):
        s.update(first, input={})


def test_scan_and_source_identity_are_transactional(tmp_path, monkeypatch):
    s, ig = collection(tmp_path)
    source = ig.add_source("pets", now=100)
    with pytest.raises(RuntimeError), s.transaction():
        ig.apply_scan(batch(source, [post()]))
        raise RuntimeError("ack fails")
    assert ig.materials() == []
    assert ig.source(source["id"])["scan_revision"] == 0
    ig.apply_scan(batch(source, [post()]))
    alias = ig.add_source("renamed", now=100)
    ig.apply_scan(batch(alias, [post("other")]))
    assert ig.source(alias["id"])["status"] == "duplicate"
    assert not ig.source(alias["id"])["active"]
    assert len(ig.materials()) == 1
    assert ig.source(source["id"])["instagram_id"] == "123"


def test_request_scan_respects_global_delays_and_pause(tmp_path):
    s, ig = collection(tmp_path)
    source = ig.add_source("pets", now=100)
    s.set_value("instagram_next_run", 1000)
    ig.request_scan("default", now=500)
    assert ig.due_source(now=500) is None
    assert ig.due_source(now=1000)["id"] == source["id"]
    s.set_paused(True, channel_id="default")
    assert ig.due_source(now=1000) is None
    assert ig.channel_state("default")["sources"] == 1


def test_instagram_schedule_uses_local_date_across_dst(tmp_path):
    s, ig = collection(tmp_path)
    s.update_profile_settings("default", {"production_type": "instagram", "schedule_time": "00:00",
                                          "timezone": "America/New_York"})
    source = ig.add_source("pets", now=100)
    ig.apply_scan(batch(source, [post("A"), post("B")]))
    before = datetime(2026, 11, 1, 5, 30, tzinfo=ZoneInfo("UTC"))
    first = schedule_due(s, now=before)
    assert schedule_due(s, now=before.replace(hour=6)) == first
    assert s.get(first)["dedup_key"] == "schedule:2026-11-01"
    second = schedule_due(s, now=before.replace(day=2, hour=6))
    assert s.get(second)["dedup_key"] == "schedule:2026-11-02"
