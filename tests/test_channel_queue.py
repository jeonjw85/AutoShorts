import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from shorts_local.config import Settings
from shorts_local.models import Story
from shorts_local.process import BlockedError
from shorts_local.store import Store

A, B = "UC" + "A" * 22, "UC" + "B" * 22


def test_enqueue_snapshots_channel_settings_and_target(tmp_path):
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    s.bind_channel("animals", A, "실제 동물 채널")
    s.update_profile_settings("animals", {"topic": "첫 동물 주제", "youtube_enabled": True})
    job = s.enqueue(channel_id="animals")
    s.update_profile_settings("animals", {"topic": "나중 주제"})
    saved = s.get(job)
    assert saved["channel_id"] == "animals"
    assert saved["target_youtube_id"] == A
    assert saved["settings"]["topic"] == "첫 동물 주제" and saved["publish"] == 1
    manual = s.enqueue(channel_id="animals", topic="이번에만 다른 주제", publish=False)
    assert s.get(manual)["settings"]["topic"] == "이번에만 다른 주제"
    assert s.get(manual)["publish"] == 0
    assert s.profile("animals").settings.topic == "나중 주제"
    assert len(s.list(channel_id="default")) == 0


def test_dedup_and_fingerprints_are_scoped(tmp_path):
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    first = s.enqueue(key="schedule:2026-10-10")
    second = s.enqueue(channel_id="animals", key="schedule:2026-10-10")
    assert first != second
    assert s.enqueue(channel_id="animals", key="schedule:2026-10-10") == second
    s.update(first, fingerprint="same-content")
    s.update(second, fingerprint="same-content")
    another = s.enqueue(channel_id="animals")
    with pytest.raises(sqlite3.IntegrityError):
        s.update(another, fingerprint="same-content")


def test_claim_respects_both_pause_levels(tmp_path):
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    first = s.enqueue()
    second = s.enqueue(channel_id="animals")
    s.set_paused(True, channel_id="default")
    assert s.claim()["id"] == second
    s.set_paused(True)
    s.set_paused(False, channel_id="default")
    assert s.claim() is None
    s.set_paused(False)
    with ThreadPoolExecutor(8) as pool:
        claims = list(pool.map(lambda _: Store(tmp_path).claim(), range(8)))
    assert [j["id"] for j in claims if j] == [first]


def test_binding_is_immutable_and_unique(tmp_path):
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    job = s.enqueue()
    with pytest.raises(BlockedError):
        s.bind_job_target(job, A)
    s.bind_channel("default", A, "기존 채널")
    assert s.bind_job_target(job, A) == A
    assert s.bind_channel("default", A, "새 표시 이름").youtube_title == "새 표시 이름"
    for channel, identity in (("default", B), ("animals", A)):
        with pytest.raises(ValueError):
            s.bind_channel(channel, identity, "다른 대상")
    with pytest.raises(BlockedError):
        s.bind_job_target(job, B)
    with pytest.raises(ValueError):
        s.update(job, channel_id="animals")
    assert s.get(job)["target_youtube_id"] == A


def test_supplied_story_is_visible_at_first_claim(tmp_path, story_data):
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    job = s.enqueue(Settings(), story=Story(**story_data), channel_id="animals")
    claimed = Store(tmp_path).claim()
    assert claimed["id"] == job
    assert Story.model_validate_json((tmp_path / "jobs" / job / "story.json").read_text()).title == "내일 도착한 편지"
    s.notify("영상 완성", job)
    assert "[동물 · animals]" in s.pending_notifications()[0]["text"]
