from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shorts_local.config import Settings, initialize
from shorts_local.process import atomic_json
from shorts_local.store import Store


def test_schedule_does_not_duplicate_after_restart_or_time_change(tmp_path):
    from shorts_local.service import schedule_due

    store = Store(tmp_path)
    now = datetime(2026, 10, 10, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    schedule_due(store, now=now)
    store.update_profile_settings("default", {"schedule_time": "11:00"})
    schedule_due(Store(tmp_path), now=now)
    assert len(store.list()) == 1
    # A three-day sleep creates today's one job, not three days of backlog.
    schedule_due(store, now=now.replace(day=13))
    assert len(store.list()) == 2


def test_schedule_respects_timezone_and_pause(tmp_path):
    from shorts_local.service import schedule_due

    store = Store(tmp_path)
    early = datetime(2026, 10, 9, 23, tzinfo=ZoneInfo("UTC"))
    schedule_due(store, now=early)
    assert not store.list()
    store.set_value("paused", True)
    schedule_due(store, now=early.replace(day=10, hour=1))
    assert not store.list()


def test_restart_skips_old_scheduled_generation_but_keeps_upload_recovery(tmp_path):
    from shorts_local.service import schedule_due

    store = Store(tmp_path)
    settings = Settings()
    old = store.enqueue(settings, key="schedule:2026-10-10")
    store.update(old, status="retry", stage="speech")
    uploading = store.enqueue(settings, key="schedule:2026-10-11", publish=True)
    folder = tmp_path / "jobs" / uploading
    atomic_json(folder / "upload-state.json", {"url": "existing session"})
    today = datetime(2026, 10, 13, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    current = schedule_due(store, now=today)
    assert store.claim(now=today.timestamp())["id"] == uploading
    assert store.get(old)["status"] == "skipped"
    assert (folder / "upload-state.json").exists()
    assert store.claim(now=today.timestamp())["id"] == current


def update(text, owner=42, update_id=100, chat_type="private"):
    return {"update_id": update_id, "message": {
        "from": {"id": owner}, "chat": {"id": owner, "type": chat_type}, "text": text,
    }}


def test_telegram_rejects_other_users_and_groups(tmp_path):
    from shorts_local.telegram import handle_update

    settings = initialize(tmp_path)
    store = Store(tmp_path)
    assert handle_update(store, update("/create", owner=7), 42) is None
    assert handle_update(store, update("/create", chat_type="group"), 42) is None
    assert not store.list()
    assert store.profile().settings == settings


def test_replayed_telegram_create_only_queues_one_job(tmp_path):
    from shorts_local.telegram import handle_update

    initialize(tmp_path)
    store = Store(tmp_path)
    for _ in range(2):
        reply = handle_update(store, update("/create 우주 우체부의 하루"), 42)
        assert "text" in reply
    assert len(store.list()) == 1
    assert store.list()[0]["settings"]["topic"] == "우주 우체부의 하루"


def test_telegram_settings_are_validated_before_saving(tmp_path):
    from shorts_local.telegram import handle_update

    initialize(tmp_path)
    store = Store(tmp_path)
    handle_update(store, update("/set schedule 29:00"), 42)
    assert store.profile().settings.schedule_time == "09:00"
    handle_update(store, update("/set topic 오래된 물건에 관한 창작 이야기", update_id=101), 42)
    assert store.profile().settings.topic == "오래된 물건에 관한 창작 이야기"


def test_replayed_retry_does_not_requeue_a_job_that_has_since_completed(tmp_path):
    from shorts_local.telegram import handle_update

    settings = initialize(tmp_path)
    store = Store(tmp_path)
    job = store.enqueue(settings)
    store.update(job, status="failed")
    command = update(f"/retry {job}")
    first = handle_update(store, command, 42)
    store.update(job, status="ready")
    assert handle_update(store, command, 42) == first
    assert store.get(job)["status"] == "ready"
    assert len(store.pending_notifications()) == 1


def test_replayed_setting_does_not_overwrite_newer_cli_configuration(tmp_path):
    from shorts_local.telegram import handle_update

    initialize(tmp_path)
    store = Store(tmp_path)
    command = update("/set topic 텔레그램 주제")
    handle_update(store, command, 42)
    store.update_profile_settings("default", {"topic": "나중에 바꾼 CLI 주제"})
    handle_update(store, command, 42)
    assert store.profile().settings.topic == "나중에 바꾼 CLI 주제"


def test_failed_preview_delivery_does_not_block_pause_or_update_ack(tmp_path):
    import httpx

    from shorts_local.service import process_telegram
    from shorts_local.telegram import Telegram

    settings = initialize(tmp_path)
    store = Store(tmp_path)
    job = store.enqueue(settings)
    folder = tmp_path / "jobs" / job
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"example preview")
    offsets = []

    def server(request):
        import json

        if request.url.path.endswith("/getUpdates"):
            offset = json.loads(request.content)["offset"]
            offsets.append(offset)
            return httpx.Response(200, json={"ok": True, "result": [
                update(f"/preview {job}", update_id=100), update("/pause", update_id=101),
            ] if offset == 0 else []})
        if request.url.path.endswith("/sendVideo"):
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        bot = Telegram("test-token", 42, client)
        process_telegram(store, bot)
        assert store.profile().paused is True
        assert store.value("telegram_offset") == 102
        process_telegram(store, bot)
    assert offsets == [0, 102]


def test_pipeline_reuses_valid_voice_and_finished_video(tmp_path, story_data):
    from shorts_local.pipeline import run_next
    from tests.test_media import write_tone

    settings = Settings(width=360, height=640, min_seconds=1)
    store = Store(tmp_path)
    job_id = store.enqueue(settings)
    folder = tmp_path / "jobs" / job_id
    (folder / "voice").mkdir(parents=True)
    atomic_json(folder / "story.json", story_data)
    for index in range(3):
        write_tone(folder / "voice" / f"{index:03}.wav")
    assert run_next(tmp_path) == job_id
    assert store.get(job_id)["status"] == "ready"
    before = (folder / "video.mp4").stat().st_mtime_ns
    store.retry(job_id)
    run_next(tmp_path)
    assert (folder / "video.mp4").stat().st_mtime_ns == before
    assert store.get(job_id)["status"] == "ready"


def test_pipeline_failure_is_persisted_not_lost(tmp_path, monkeypatch):
    import shorts_local.pipeline as pipeline

    store = Store(tmp_path)
    job_id = store.enqueue(Settings())

    def fail(*args):
        raise RuntimeError("temporary model failure")

    monkeypatch.setattr(pipeline, "plan_story", fail)
    pipeline.run_next(tmp_path)
    job = store.get(job_id)
    assert job["status"] == "retry"
    assert job["stage"] == "plan"
    assert job["attempts"] == 1
    assert "temporary model failure" in job["error"]


def test_corrupt_images_are_regenerated_before_rendering(tmp_path, story_data, monkeypatch):
    from PIL import Image

    import shorts_local.pipeline as pipeline
    from tests.test_media import write_tone

    settings = Settings(visuals="mflux", width=360, height=640, min_seconds=1)
    store = Store(tmp_path)
    job = store.enqueue(settings)
    folder = tmp_path / "jobs" / job
    (folder / "voice").mkdir(parents=True)
    (folder / "images").mkdir()
    atomic_json(folder / "story.json", story_data)
    for index in range(3):
        write_tone(folder / "voice" / f"{index:03}.wav")
        (folder / "images" / f"{index:03}.png").write_bytes(b"truncated image")
    calls = []

    def engine(stage, workdir, settings):
        calls.append(stage)
        for index in range(3):
            Image.new("RGB", (576, 1024), "navy").save(workdir / "images" / f"{index:03}.png")

    monkeypatch.setattr(pipeline, "engine", engine)
    pipeline.run_next(tmp_path)
    assert calls == ["images"]
    assert store.get(job)["status"] == "ready"


def test_selected_channel_commands_are_isolated(tmp_path):
    from shorts_local.telegram import handle_update

    s = Store(tmp_path)
    for number, text in enumerate(("/channel_add animals 동물", "/channel animals",
                                   "/set topic 동물 주제", "/create", "/channel_add stories 이야기"), 100):
        reply = handle_update(s, update(text, update_id=number), 42)
        assert "animals" in reply["text"] or "stories" in reply["text"]
    assert s.list()[0]["channel_id"] == "animals"
    assert s.profile("animals").settings.topic == "동물 주제"
    assert s.profile().settings.topic != "동물 주제"
    assert Store(tmp_path).value("telegram_channel") == "animals"


def test_replay_preserves_current_channel_and_original_reply(tmp_path):
    from shorts_local.telegram import handle_update

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    command = update("/create", update_id=100)
    reply = handle_update(s, command, 42)
    selection = update("/channel animals", update_id=101)
    selected = handle_update(s, selection, 42)
    assert handle_update(s, command, 42) == reply
    assert s.value("telegram_channel") == "animals"
    assert len(s.list()) == 1 and s.list()[0]["channel_id"] == "default"
    handle_update(s, update("/channel default", update_id=102), 42)
    assert handle_update(s, selection, 42) == selected
    assert s.value("telegram_channel") == "default"


@pytest.mark.parametrize("text", ["/set topic 변경한 주제", "/channel animals", "/create"])
def test_command_rolls_back_settings_when_ack_fails(tmp_path, monkeypatch, text):
    from shorts_local.telegram import handle_update

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    original = s.profile().settings
    ack = s.accept_telegram

    def fail(*args):
        raise RuntimeError("ack failed")

    monkeypatch.setattr(s, "accept_telegram", fail)
    with pytest.raises(RuntimeError):
        handle_update(s, update(text), 42)
    assert s.profile().settings == original
    assert s.value("telegram_channel", "default") == "default"
    assert not s.list() and not s.pending_notifications()
    monkeypatch.setattr(s, "accept_telegram", ack)
    reply = handle_update(s, update(text), 42)
    assert handle_update(s, update(text), 42) == reply
    assert len(s.pending_notifications()) == 1
    if text == "/create":
        assert len(s.list()) == 1
    elif text.startswith("/set"):
        assert s.profile().settings.topic == "변경한 주제"
    else:
        assert s.value("telegram_channel") == "animals"


def test_telegram_pause_and_job_id_scope(tmp_path):
    from shorts_local.telegram import handle_update

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    job = s.enqueue()
    s.update(job, status="ready")
    s.set_value("telegram_channel", "animals")
    handle_update(s, update("/pause"), 42)
    assert s.profile("animals").paused and not s.profile().paused
    assert not s.value("paused", False)
    handle_update(s, update("/pause all", update_id=101), 42)
    assert s.value("paused")
    reply = handle_update(s, update(f"/upload {job}", update_id=102), 42)
    assert "default" in reply["text"] and s.get(job)["publish"] == 1
    assert s.value("telegram_channel") == "animals"


def test_invalid_or_unauthorized_commands_do_not_change_channel(tmp_path):
    from shorts_local.telegram import handle_update

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    s.set_value("telegram_channel", "animals")
    assert handle_update(s, update("/channel default", owner=7), 42) is None
    assert handle_update(s, update("/channel default", chat_type="group"), 42) is None
    for number, text in enumerate(("/channel missing", "/set schedule 29:00",
                                   "/set schedule_enabled maybe"), 100):
        handle_update(s, update(text, update_id=number), 42)
    assert s.value("telegram_channel") == "animals"
    assert s.profile("animals").settings.schedule_time == "09:00"
    assert s.profile("animals").settings.schedule_enabled
