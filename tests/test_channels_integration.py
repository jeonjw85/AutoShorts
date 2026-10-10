import hashlib
import json
from datetime import datetime

import httpx
import pytest

from shorts_local.config import Settings
from shorts_local.models import Story
from shorts_local.process import BlockedError, atomic_json
from shorts_local.store import Store
from tests.test_media import write_tone
from tests.test_youtube import SESSION, A, B


def credential_info(token):
    return {"token": token, "refresh_token": "fixture-refresh", "client_id": "fixture",
            "client_secret": "fixture-secret", "token_uri": "https://oauth2.googleapis.com/token",
            "expiry": "2099-01-01T00:00:00Z"}


def connect(s, channel, identity):
    s.bind_channel(channel, identity, channel)
    s.update_profile_settings(channel, {"youtube_enabled": True})
    atomic_json(s.root / "channels" / channel / "auth/youtube.json", {
        "youtube_channel_id": identity, "credentials": credential_info(identity),
    })


def youtube_network(monkeypatch):
    original = httpx.Client
    transfers = []

    def server(request):
        identity = request.headers["authorization"].removeprefix("Bearer ")
        if request.url.path == "/youtube/v3/channels":
            return httpx.Response(200, json={"items": [{"id": identity, "snippet": {"title": "test"}}]})
        transfers.append((request.method, identity))
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(200, json={"id": "video123456"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(server)))
    return transfers


def prepare_media(s, job, story_data, *, story=True):
    folder = s.root / "jobs" / job
    (folder / "voice").mkdir(parents=True)
    if story:
        atomic_json(folder / "story.json", story_data)
    for index in range(3):
        write_tone(folder / "voice" / f"{index:03}.wav")
    return folder


def test_two_channels_schedule_once_and_pause_independently(tmp_path):
    from shorts_local.service import schedule_due

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    now = datetime.fromisoformat("2026-10-10T12:00:00+09:00")
    for channel in ("default", "animals"):
        first = schedule_due(s, channel, now)
        assert schedule_due(Store(tmp_path), channel, now) == first
    assert len(s.list()) == 2
    s.set_paused(True, channel_id="default")
    assert schedule_due(s, "default", now.replace(day=11)) is None
    assert schedule_due(s, "animals", now.replace(day=11))
    assert len(s.list()) == 3


def test_schedule_uses_each_local_date_across_dst(tmp_path):
    from shorts_local.service import schedule_due

    s = Store(tmp_path)
    s.add_profile("la", "LA")
    s.update_profile_settings("default", {"schedule_time": "01:00"})
    s.update_profile_settings("la", {"timezone": "America/Los_Angeles", "schedule_time": "08:00"})
    now = datetime.fromisoformat("2026-11-01T16:30:00+00:00")
    seoul, la = schedule_due(s, "default", now), schedule_due(s, "la", now)
    assert s.get(seoul)["dedup_key"] == "schedule:2026-11-02"
    assert s.get(la)["dedup_key"] == "schedule:2026-11-01"
    s.update_profile_settings("la", {"schedule_time": "01:00"})
    for hour in (8, 9):
        assert schedule_due(s, "la", now.replace(hour=hour)) == la


def test_pipeline_uses_job_profile_after_selection_and_settings_change(tmp_path, story_data, monkeypatch):
    import shorts_local.pipeline as pipeline

    transfers = youtube_network(monkeypatch)
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    connect(s, "animals", A)
    s.update_profile_settings("animals", {"width": 360, "height": 640, "min_seconds": 1, "topic": "원래 동물 주제"})
    previous = s.enqueue(channel_id="animals", publish=False)
    s.update(previous, status="ready", title="동물의 옛 제목")
    other = s.enqueue()
    s.update(other, status="ready", title="다른 채널 제목")
    job = s.enqueue(channel_id="animals")
    prepare_media(s, job, story_data, story=False)
    s.update_profile_settings("animals", {"topic": "변경한 주제"})
    s.set_value("telegram_channel", "default")

    def planner(settings, recent, folder):
        assert settings.topic == "원래 동물 주제"
        assert recent == ["동물의 옛 제목"]
        return Story(**story_data)

    monkeypatch.setattr(pipeline, "plan_story", planner)
    assert pipeline.run_next(tmp_path) == job
    assert s.get(job)["status"] == "uploaded", s.get(job)["error"]
    assert transfers == [("POST", A), ("PUT", A)]
    assert "animals" in s.pending_notifications()[0]["text"]


def test_blocked_channel_does_not_block_next_channel(tmp_path, story_data):
    from shorts_local.pipeline import run_next

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    settings = Settings(width=360, height=640, min_seconds=1)
    first = s.enqueue(settings, publish=True)
    second = s.enqueue(settings, channel_id="animals", publish=False)
    for job in (first, second):
        prepare_media(s, job, story_data)
    assert run_next(tmp_path) == first
    assert s.get(first)["status"] == "blocked"
    assert run_next(tmp_path) == second
    assert s.get(second)["status"] == "ready"
    notes = s.pending_notifications()
    assert "default" in notes[0]["text"] and "animals" in notes[1]["text"]


@pytest.mark.parametrize("case", ["valid", "missing", "different"])
def test_legacy_upload_requires_original_credentials(legacy_home, story_data, monkeypatch, case):
    from shorts_local.pipeline import publish_video

    transfers = youtube_network(monkeypatch)
    s = Store(legacy_home)
    folder = legacy_home / "jobs/legacy-upload"
    video = folder / "video.mp4"
    video.write_bytes(b"video")
    atomic_json(folder / "upload-state.json", {
        "url": SESSION, "size": 5, "sha256": hashlib.sha256(b"video").hexdigest(),
    })
    atomic_json(legacy_home / "auth/youtube.json", credential_info(A))
    atomic_json(legacy_home / "channels/default/auth/youtube.json", credential_info(A))
    if case == "different":
        connect(s, "default", B)
    if case == "missing":
        (legacy_home / "auth/youtube.json").unlink()
    job = s.get("legacy-upload")
    if case == "valid":
        assert publish_video(s, job, video, Story(**story_data), folder) == "video123456"
        assert transfers == [("PUT", A)]
        assert s.get(job["id"])["target_youtube_id"] == A
    else:
        with pytest.raises(BlockedError):
            publish_video(s, job, video, Story(**story_data), folder)
        assert transfers == []
        atomic_json(folder / "upload-receipt.json", {
            "sha256": hashlib.sha256(b"video").hexdigest(), "video_id": "video123456",
        })
        assert publish_video(s, job, video, Story(**story_data), folder) == "video123456"
        assert transfers == []


def test_unbound_saved_credentials_cannot_start_upload(tmp_path, story_data, monkeypatch):
    from shorts_local.pipeline import publish_video

    transfers = youtube_network(monkeypatch)
    s = Store(tmp_path)
    job = s.enqueue(publish=True)
    folder = prepare_media(s, job, story_data)
    video = folder / "video.mp4"
    video.write_bytes(b"video")
    atomic_json(tmp_path / "channels/default/auth/youtube.json", {
        "youtube_channel_id": A, "credentials": credential_info(A),
    })
    with pytest.raises(BlockedError):
        publish_video(s, s.get(job), video, Story(**story_data), folder)
    assert transfers == []


def test_two_channels_survive_restart_and_publish_to_original_targets(tmp_path, story_data, monkeypatch):
    from types import SimpleNamespace

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from typer.testing import CliRunner

    from shorts_local.cli import app
    from shorts_local.pipeline import publish_video, run_next
    from shorts_local.telegram import handle_update
    from tests.test_service import update

    transfers = youtube_network(monkeypatch)

    def flow(path, scopes):
        identity = B if path.endswith("animals.json") else A
        creds = Credentials.from_authorized_user_info(credential_info(identity), scopes=scopes)
        return SimpleNamespace(run_local_server=lambda **_: creds)

    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", flow)
    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    assert runner.invoke(app, home + ["init"]).exit_code == 0
    assert runner.invoke(app, home + ["channel", "add", "animals", "--name", "동물"]).exit_code == 0
    for channel in ("default", "animals"):
        client_file = tmp_path / f"{channel}.json"
        client_file.write_text("{}")
        result = runner.invoke(app, home + ["youtube-login", str(client_file), "--channel", channel])
        assert result.exit_code == 0, result.output
        assert runner.invoke(app, home + ["config", "--channel", channel, "min_seconds", "1"]).exit_code == 0
        # Use smaller paired dimensions for the real FFmpeg integration test.
        Store(tmp_path).update_profile_settings(channel, {"width": 360, "height": 640})
    s = Store(tmp_path)
    for number, command in enumerate(("/channel default", "/create", "/channel animals", "/create",
                                       "/set topic 다음 영상의 새 주제"), 100):
        handle_update(s, update(command, update_id=number), 42)
    jobs = s.list()
    assert {j["channel_id"] for j in jobs} == {"default", "animals"}
    for job in jobs:
        assert job["settings"]["topic"] != "다음 영상의 새 주제"
        prepare_media(s, job["id"], story_data)
    s = Store(tmp_path)
    assert s.value("telegram_channel") == "animals"
    for _ in jobs:
        assert run_next(tmp_path)
    assert transfers == [("POST", A), ("PUT", A), ("POST", B), ("PUT", B)]
    before = list(transfers)
    for job in s.list():
        assert job["status"] == "uploaded", job["error"]
        folder = tmp_path / "jobs" / job["id"]
        receipt = json.loads((folder / "upload-receipt.json").read_text())
        assert receipt["channel_id"] == job["channel_id"]
        assert receipt["youtube_channel_id"] == (A if job["channel_id"] == "default" else B)
        (tmp_path / "channels" / job["channel_id"] / "auth/youtube.json").unlink()
        assert publish_video(s, job, folder / "video.mp4", Story(**story_data), folder) == "video123456"
    assert transfers == before


def test_interrupted_reconcile_recovers_on_ordinary_retry(legacy_home, story_data, monkeypatch):
    from typer.testing import CliRunner

    from shorts_local.cli import app
    from shorts_local.pipeline import publish_video

    transfers = youtube_network(monkeypatch)
    s = Store(legacy_home)
    s.bind_channel("default", A, "원래 채널")
    job_id = "legacy-upload"
    s.update(job_id, status="blocked")
    assert s.get(job_id)["target_youtube_id"] is None
    folder = legacy_home / "jobs" / job_id
    video = folder / "video.mp4"
    video.write_bytes(b"video")
    original_update = Store.update

    def interrupted(self, job_id, **values):
        if values.get("status") == "uploaded":
            raise RuntimeError("interrupted after receipt before commit")
        return original_update(self, job_id, **values)

    monkeypatch.setattr(Store, "update", interrupted)
    result = CliRunner().invoke(app, ["--home", str(legacy_home), "reconcile", job_id, "video123456"])
    assert result.exit_code != 0
    assert json.loads((folder / "upload-receipt.json").read_text())["youtube_channel_id"] == A
    assert s.get(job_id)["status"] == "blocked"
    monkeypatch.setattr(Store, "update", original_update)
    s.retry(job_id)
    restarted = Store(legacy_home)
    assert publish_video(restarted, restarted.get(job_id), video, Story(**story_data), folder) == "video123456"
    assert restarted.get(job_id)["target_youtube_id"] == A
    assert transfers == []
