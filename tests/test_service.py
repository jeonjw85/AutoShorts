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


def test_serve_without_bot_writes_heartbeat_and_drains_alerts(tmp_path, monkeypatch):
    import threading

    from shorts_local import service
    from shorts_local.health import read_heartbeat
    from shorts_local.store import Store

    class FakeTime:
        @staticmethod
        def time():
            return 1000.0

        @staticmethod
        def sleep(seconds):
            assert seconds == 5
            stop.set()

    stop = threading.Event()
    monkeypatch.setattr(service, "time", FakeTime)
    monkeypatch.setattr(service, "credentials", lambda root: None)
    monkeypatch.setattr(service, "run_next", lambda root: None)
    monkeypatch.setattr(service, "run_collection", lambda root: None)
    monkeypatch.setattr(service, "run_news_collection", lambda root: None)
    store = Store(tmp_path)
    store.notify("알림")
    service.serve(tmp_path, stop)
    heartbeat = read_heartbeat(tmp_path)
    assert heartbeat["pid"] > 0 and heartbeat["updated_at"] == 1000.0
    assert store.notification_count() == 0
    assert "알림" in (tmp_path / "alerts.log").read_text()


def test_serve_survives_heartbeat_write_failure(tmp_path, monkeypatch):
    import threading

    from shorts_local import service
    from shorts_local.health import read_heartbeat
    from shorts_local.store import Store

    class FakeTime:
        @staticmethod
        def time():
            return 1000.0

        @staticmethod
        def sleep(seconds):
            stop.set()

    def broken_heartbeat(*args, **kwargs):
        raise OSError("read-only file system")

    stop = threading.Event()
    monkeypatch.setattr(service, "time", FakeTime)
    monkeypatch.setattr(service, "write_heartbeat", broken_heartbeat)
    monkeypatch.setattr(service, "credentials", lambda root: None)
    monkeypatch.setattr(service, "run_next", lambda root: None)
    monkeypatch.setattr(service, "run_collection", lambda root: None)
    monkeypatch.setattr(service, "run_news_collection", lambda root: None)
    service.serve(tmp_path, stop)
    assert read_heartbeat(tmp_path) is None
    assert Store(tmp_path).value("paused", False) is False


def test_telegram_status_reports_service_and_pending_alerts(tmp_path):
    from shorts_local.store import Store
    from shorts_local.telegram import handle_update

    store = Store(tmp_path)
    store.notify("대기 알림")
    reply = handle_update(store, update("/status"), 42)
    assert "서비스:" in reply["text"]
    assert "미전송 알림: 1건" in reply["text"]


def test_serve_runs_the_token_age_check(tmp_path, monkeypatch):
    import threading

    from shorts_local import service
    from shorts_local.store import Store

    class FakeTime:
        @staticmethod
        def time():
            return 10000.0

        @staticmethod
        def sleep(seconds):
            stop.set()

    stop = threading.Event()
    calls = []
    monkeypatch.setattr(service, "time", FakeTime)
    monkeypatch.setattr(service, "credentials", lambda root: None)
    monkeypatch.setattr(service, "run_next", lambda root: None)
    monkeypatch.setattr(service, "run_collection", lambda root: None)
    monkeypatch.setattr(service, "run_news_collection", lambda root: None)
    monkeypatch.setattr(service, "warn_expiring_tokens", lambda store, now=None: calls.append(now))
    Store(tmp_path)
    service.serve(tmp_path, stop)
    assert calls == [10000.0]


def _serve_time(monkeypatch, service, stop):
    class FakeTime:
        @staticmethod
        def time():
            return 1000.0

        @staticmethod
        def sleep(seconds):
            stop.set()

    monkeypatch.setattr(service, "time", FakeTime)
    monkeypatch.setattr(service, "credentials", lambda root: None)
    monkeypatch.setattr(service, "run_next", lambda root: None)
    monkeypatch.setattr(service, "run_collection", lambda root: None)
    monkeypatch.setattr(service, "run_news_collection", lambda root: None)


def test_serve_survives_a_notification_drain_failure(tmp_path, monkeypatch):
    import sqlite3
    import threading

    from shorts_local import service
    from shorts_local.store import Store

    def broken_drain(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    stop = threading.Event()
    _serve_time(monkeypatch, service, stop)
    monkeypatch.setattr(service, "drain_notifications", broken_drain)
    Store(tmp_path)
    service.serve(tmp_path, stop)


def test_serve_survives_a_queue_depth_failure(tmp_path, monkeypatch):
    import sqlite3
    import threading

    from shorts_local import service
    from shorts_local.health import read_heartbeat
    from shorts_local.store import Store

    def broken_depth(self):
        raise sqlite3.OperationalError("database is locked")

    stop = threading.Event()
    _serve_time(monkeypatch, service, stop)
    monkeypatch.setattr(Store, "queue_depth", broken_depth)
    Store(tmp_path)
    service.serve(tmp_path, stop)
    assert read_heartbeat(tmp_path) is None


def test_serve_survives_a_scheduling_failure(tmp_path, monkeypatch):
    import sqlite3
    import threading

    from shorts_local import service
    from shorts_local.store import Store

    def broken_profiles(self):
        raise sqlite3.OperationalError("database is locked")

    stop = threading.Event()
    _serve_time(monkeypatch, service, stop)
    monkeypatch.setattr(Store, "profiles", broken_profiles)
    Store(tmp_path)
    service.serve(tmp_path, stop)


def test_serve_survives_a_telegram_processing_failure(tmp_path, monkeypatch):
    import sqlite3
    import threading

    from shorts_local import service
    from shorts_local.store import Store

    class FakeBot:
        def close(self):
            pass

    def broken_process(store, bot):
        raise sqlite3.OperationalError("database is locked")

    stop = threading.Event()
    _serve_time(monkeypatch, service, stop)
    monkeypatch.setattr(service, "credentials", lambda root: ("id", "token"))
    monkeypatch.setattr(service, "Telegram", lambda *auth: FakeBot())
    monkeypatch.setattr(service, "process_telegram", broken_process)
    Store(tmp_path)
    service.serve(tmp_path, stop)
