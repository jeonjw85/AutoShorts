import json

import httpx
import pytest

from shorts_local.config import Settings
from shorts_local.instagram_store import InstagramStore
from shorts_local.models import Story
from shorts_local.pipeline import execute_job, run_next
from shorts_local.process import atomic_json
from shorts_local.service import schedule_due
from shorts_local.store import Store
from shorts_local.telegram import handle_update
from tests.instagram_helpers import batch, make_source, post
from tests.test_channels_integration import connect, prepare_media, youtube_network
from tests.test_instagram_render import edit, tone_at
from tests.test_media import write_tone
from tests.test_service import update
from tests.test_youtube import SESSION, A, B


def local_input(store, job):
    folder = store.root / "jobs" / job
    source = make_source(folder)
    atomic_json(folder / "source.json", source.model_dump())
    return folder


def local_models(monkeypatch):
    import shorts_local.editing as editing
    import shorts_local.import_pipeline as imported

    monkeypatch.setattr(imported, "analyze_source", lambda *a: edit()[1])
    monkeypatch.setattr(editing, "generate_draft", lambda settings, *a, **kw: edit(settings.instagram_edit_mode)[0].model_dump_json())
    original = editing.run_process
    def speech(args, log, timeout, **kwargs):
        if "edit-speech" not in args:
            return original(args, log, timeout, **kwargs)
        request = json.loads((log.parent / "speech-request.json").read_text())
        folder = log.parent / request["voice_subdir"]
        folder.mkdir(parents=True, exist_ok=True)
        for index, _text in enumerate(request["texts"]):
            write_tone(folder / f"{index:03}.wav", 2)
    monkeypatch.setattr(editing, "run_process", speech)


def test_two_channels_import_collect_restart_and_publish(tmp_path, monkeypatch, story_data):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    local_models(monkeypatch)
    store = Store(tmp_path)
    store.add_profile("other", "다른 채널")
    for channel, identity, mode in (("default", A, "narrated"), ("other", B, "original")):
        connect(store, channel, identity)
        store.update_profile_settings(channel, {"width": 360, "height": 640,
                                                "production_type": "instagram", "instagram_edit_mode": mode})
    ig = InstagramStore(store)
    first = ig.import_video(post().url)
    for channel in ("default", "other"):
        source = ig.add_source("pets", channel_id=channel, now=100)
        ig.apply_scan(batch(source, [post()]))
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(hour=23, minute=59)
    second = schedule_due(store, "other", now=now)
    assert ig.import_video(post().url) == first
    for job in (first, second):
        local_input(store, job)
    posts, queries, completed = [], [], set()
    client_class = httpx.Client
    def server(request):
        identity = request.headers["authorization"].removeprefix("Bearer ")
        if request.url.path == "/youtube/v3/channels":
            return httpx.Response(200, json={"items": [{"id": identity, "snippet": {"title": "test"}}]})
        if request.method == "POST":
            posts.append(identity)
            return httpx.Response(200, headers={"Location": SESSION + identity})
        if request.headers["content-range"].startswith("bytes */"):
            queries.append(str(request.url))
            assert identity in completed
            return httpx.Response(200, json={"id": "video123456"})
        completed.add(identity)
        if identity == A:
            raise httpx.ReadTimeout("signed-url=SIGNEDSECRET")
        return httpx.Response(200, json={"id": "video123456"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=httpx.MockTransport(server)))
    run_next(tmp_path)
    assert store.get(first)["status"] == "retry", store.get(first)["error"]
    run_next(tmp_path)
    assert store.get(second)["status"] == "uploaded", store.get(second)["error"]
    store.update_profile_settings("default", {"instagram_edit_mode": "original", "instagram_style": "변경 후 스타일"})
    store.set_value("telegram_channel", "other")
    store.update(first, next_run=0)
    assert run_next(tmp_path) == first
    assert Store(tmp_path).get(first)["status"] == "uploaded"
    assert posts == [A, B] and queries == [SESSION + A]
    assert len(store.list()) == 2
    for job in (first, second):
        folder = tmp_path / "jobs" / job
        assert tone_at(folder / "video.mp4", 0.5)[0] == pytest.approx(440, abs=5)
        seal = json.loads((folder / "validation.json").read_text())
        assert seal["kind"] == "instagram-final"
    story = store.enqueue(Settings(width=360, height=640, min_seconds=1), story=Story(**story_data), publish=False)
    prepare_media(store, story, story_data, story=False)
    assert run_next(tmp_path) == story and store.get(story)["status"] == "ready"
    assert "SIGNEDSECRET" not in json.dumps(store.pending_notifications())


@pytest.mark.parametrize("failure", ["auth_wait", "rate_limit", "transient"])
def test_collection_failures_leave_bot_and_ready_upload_operational(tmp_path, monkeypatch, failure):
    local_models(monkeypatch)
    store = Store(tmp_path)
    store.update_profile_settings("default", {"width": 360, "height": 640, "instagram_edit_mode": "original"})
    connect(store, "default", A)
    ig = InstagramStore(store)
    job = ig.import_video(post().url, publish=False)
    local_input(store, job)
    run_next(tmp_path)
    assert store.get(job)["status"] == "ready"
    source = ig.add_source("pets", now=100)
    ig.record_failure(source["id"], failure)
    reply = handle_update(store, update(f"/upload {job}", update_id=123), 42)
    assert "original" in reply["text"] and store.value("telegram_offset") == 124
    transfers = youtube_network(monkeypatch)
    run_next(tmp_path)
    assert store.get(job)["status"] == "uploaded"
    assert transfers == [("POST", A), ("PUT", A)]


def test_schema1_home_runs_both_pipelines_after_upgrade(schema1_home, monkeypatch, story_data):
    import sqlite3

    root = schema1_home
    before = (root / "jobs/legacy-ready/video.mp4").read_bytes()
    local_models(monkeypatch)
    store = Store(root)
    offset = store.value("telegram_offset")
    original = store.get("legacy-ready")
    store.update_profile_settings("default", {"width": 360, "height": 640, "instagram_edit_mode": "original"})
    job = InstagramStore(store).import_video(post().url, publish=False)
    local_input(store, job)
    execute_job(store, store.get(job))
    story = store.enqueue(Settings(width=360, height=640, min_seconds=1), story=Story(**story_data), publish=False)
    prepare_media(store, story, story_data, story=False)
    execute_job(store, store.get(story))
    assert store.get(story)["status"] == store.get(job)["status"] == "ready"
    assert store.get("legacy-ready") == original and store.value("telegram_offset") == offset
    assert (root / "jobs/legacy-ready/video.mp4").read_bytes() == before
    for path in (root / "state.sqlite3", root / "backups/pre-channels.sqlite3", root / "backups/pre-instagram.sqlite3"):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
