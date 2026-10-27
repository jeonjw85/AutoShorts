import json
import shutil

import pytest
from typer.testing import CliRunner


def test_init_and_queue_example_without_network_or_models(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    assert runner.invoke(app, [*args, "init"]).exit_code == 0
    result = runner.invoke(app, [*args, "create", "--story", "examples/letter.json", "--local-only"])
    assert result.exit_code == 0, result.output
    jobs = Store(tmp_path).list()
    assert len(jobs) == 1
    assert jobs[0]["publish"] == 0
    assert (tmp_path / "jobs" / jobs[0]["id"] / "story.json").exists()


def test_cli_rejects_paid_provider_and_keeps_previous_config(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    result = runner.invoke(app, [*args, "config", "planner", "paid-api"])
    assert result.exit_code != 0
    assert Store(tmp_path).profile().settings.planner == "mlx"


def test_supplied_story_exists_when_another_worker_can_first_claim_it(tmp_path, monkeypatch):
    from shorts_local.cli import app
    from shorts_local.models import Story
    from shorts_local.store import Store

    original_enqueue = Store.enqueue
    observed = []

    def enqueue_then_claim(self, *args, **kwargs):
        job_id = original_enqueue(self, *args, **kwargs)
        job = Store(self.root).claim()
        story = self.root / "jobs" / job["id"] / "story.json"
        observed.append(Story.model_validate_json(story.read_text()).title if story.exists() else None)
        return job_id

    monkeypatch.setattr(Store, "enqueue", enqueue_then_claim)
    result = CliRunner().invoke(app, ["--home", str(tmp_path), "create", "--story", "examples/letter.json"])
    assert result.exit_code == 0, result.output
    assert observed == ["내일 도착한 편지"]


@pytest.mark.parametrize("command,status,stage", [("publish", "ready", "complete"),
                                                   ("retry", "failed", "speech")])
def test_explicit_request_can_run_an_old_scheduled_job(tmp_path, command, status, stage):
    from shorts_local.cli import app
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    job = store.enqueue(Settings(), key="schedule:2020-01-01")
    store.update(job, status=status, stage=stage)
    result = CliRunner().invoke(app, ["--home", str(tmp_path), command, job])
    assert result.exit_code == 0, result.output
    claimed = Store(tmp_path).claim()
    assert claimed is not None
    assert claimed["id"] == job
    # Keep the scheduling key so an explicit same-day retry cannot create a second reservation.
    assert store.enqueue(Settings(), key="schedule:2020-01-01") == job


def test_service_file_quotes_paths_and_does_not_install_itself(tmp_path):
    import plistlib

    from shorts_local.cli import app

    root = tmp_path / "folder with spaces"
    result = CliRunner().invoke(app, ["--home", str(root), "service-file"])
    assert result.exit_code == 0, result.output
    data = plistlib.loads((root / "dev.shorts-local.plist").read_bytes())
    assert str(root) in data["ProgramArguments"]
    assert data["RunAtLoad"] is True


def test_cli_channel_roundtrip_without_models(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    for args in (["init"], ["channel", "add", "animals", "--name", "귀여운 동물"],
                 ["config", "--channel", "animals", "topic", "동물 이야기"],
                 ["create", "--channel", "animals", "--story", "examples/letter.json", "--local-only"]):
        result = runner.invoke(app, home + args)
        assert result.exit_code == 0, result.output
    s = Store(tmp_path)
    job = s.list()[0]
    assert job["channel_id"] == "animals" and job["publish"] == 0
    assert job["settings"]["topic"] == "동물 이야기"
    assert s.profile().settings.topic != "동물 이야기"
    assert "animals" in runner.invoke(app, home + ["status", "--all"]).output
    assert "animals" in runner.invoke(app, home + ["channel", "list"]).output


def test_cli_default_does_not_follow_telegram_selection(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    s.set_value("telegram_channel", "animals")
    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    assert runner.invoke(app, home + ["create", "--local-only"]).exit_code == 0
    assert s.list()[0]["channel_id"] == "default"
    for args in (["create", "--channel", "missing"], ["create", "--channel", "../other"],
                 ["status", "--all", "--channel", "animals"],
                 ["pause", "--all", "--channel", "animals"]):
        assert runner.invoke(app, home + args).exit_code != 0
    assert len(s.list()) == 1


def test_cli_pause_semantics_and_doctor(tmp_path):
    import json

    from shorts_local.cli import app
    from shorts_local.process import atomic_json
    from shorts_local.store import Store
    from tests.test_channels_integration import credential_info
    from tests.test_youtube import A

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    assert runner.invoke(app, home + ["pause"]).exit_code == 0
    assert runner.invoke(app, home + ["resume", "--channel", "animals"]).exit_code == 0
    assert s.value("paused") is True
    assert runner.invoke(app, home + ["resume", "--all"]).exit_code == 0
    assert runner.invoke(app, home + ["pause", "--channel", "animals"]).exit_code == 0
    assert not s.value("paused") and s.profile("animals").paused
    atomic_json(tmp_path / "channels/animals/auth/youtube.json", {
        "youtube_channel_id": A, "credentials": credential_info(A),
    })
    result = runner.invoke(app, home + ["doctor"])
    assert result.exit_code == 0, result.output
    channels = json.loads(result.output)["channels"]
    assert {p["id"] for p in channels} == {"default", "animals"}
    assert all(not p["youtube_connected"] for p in channels)
    assert "fixture-secret" not in result.output


def test_cli_publish_and_reconcile_use_original_job(tmp_path):
    import json

    from shorts_local.cli import app
    from shorts_local.store import Store
    from tests.test_youtube import A

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    s.bind_channel("animals", A, "동물")
    job = s.enqueue(channel_id="animals", publish=False)
    s.update(job, status="ready", stage="complete")
    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    result = runner.invoke(app, home + ["publish", job])
    assert result.exit_code == 0, result.output
    assert s.get(job)["publish"] == 1 and s.get(job)["channel_id"] == "animals"
    folder = tmp_path / "jobs" / job
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"video")
    s.update(job, status="blocked", stage="upload")
    result = runner.invoke(app, home + ["reconcile", job, "video123456"])
    assert result.exit_code == 0, result.output
    receipt = json.loads((folder / "upload-receipt.json").read_text())
    assert receipt["channel_id"] == "animals" and receipt["youtube_channel_id"] == A
    assert s.get(job)["status"] == "uploaded"


def test_cli_updates_coupled_settings_atomically(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    runner = CliRunner()
    base = ["--home", str(tmp_path), "config", "--channel", "animals"]
    result = runner.invoke(app, [*base, "--values", '{"width":720,"height":1280}'])
    assert result.exit_code == 0, result.output
    assert (s.profile("animals").settings.width, s.profile("animals").settings.height) == (720, 1280)
    assert s.profile().settings.width == 1080
    before = s.profile("animals").settings
    for value in ('{"width":360,"height":1280,"topic":"덮어쓰면 안 되는 주제"}',
                  '[{"width":360}]', '{"unknown":1}', 'broken json'):
        assert runner.invoke(app, [*base, "--values", value]).exit_code != 0
        assert s.profile("animals").settings == before
    assert runner.invoke(app, [*base, "topic", "새 주제", "--values", '{"width":720}']).exit_code != 0
    assert s.profile("animals").settings == before


def test_reconcile_cannot_race_a_running_worker(tmp_path):
    from shorts_local.cli import app
    from shorts_local.process import exclusive_lock
    from shorts_local.store import Store
    from tests.test_youtube import A

    s = Store(tmp_path)
    job = s.enqueue(publish=True)
    s.bind_channel("default", A, "원래 채널")
    s.update(job, status="blocked", stage="upload")
    folder = tmp_path / "jobs" / job
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"video")
    with exclusive_lock(tmp_path / "worker.lock"):
        result = CliRunner().invoke(app, ["--home", str(tmp_path), "reconcile", job, "video123456"])
    assert result.exit_code != 0
    assert s.get(job)["target_youtube_id"] is None
    assert s.get(job)["status"] == "blocked"
    assert not (folder / "upload-receipt.json").exists()


def test_cleanup_previews_by_default_and_applies_on_request(tmp_path):
    import time

    from shorts_local.cli import app
    from shorts_local.config import Settings
    from shorts_local.process import atomic_json
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    store = Store(tmp_path)
    job = store.enqueue(Settings())
    folder = tmp_path / "jobs" / job
    (folder / "voice").mkdir(parents=True)
    atomic_json(folder / "validation.json", {"duration": 1})
    (folder / "video.mp4").write_bytes(b"x")
    store.update(job, status="ready")
    with store.db() as db:
        db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 30 * 86400, job))

    preview = runner.invoke(app, [*args, "cleanup"])
    assert preview.exit_code == 0, preview.output
    assert "voice" in preview.output and "--apply" in preview.output
    assert (folder / "voice").exists()

    applied = runner.invoke(app, [*args, "cleanup", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert not (folder / "voice").exists()
    assert (folder / "video.mp4").exists()


def test_status_reports_heartbeat_and_pending_alerts(tmp_path):
    from shorts_local.cli import app
    from shorts_local.health import write_heartbeat
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    Store(tmp_path).notify("대기 알림")

    missing = runner.invoke(app, [*args, "status"])
    assert missing.exit_code == 0, missing.output
    assert "서비스: 기록 없음" in missing.output
    assert "미전송 알림: 1건" in missing.output

    write_heartbeat(tmp_path, version="0.1.3", pid=1, started_at=0,
                    last_job_id=None, queue_depth=0, now=0)
    assert "중단 의심" in runner.invoke(app, [*args, "status"]).output


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 필요")
def test_doctor_reports_heartbeat_and_pending_alerts(tmp_path):
    from shorts_local.cli import app
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    Store(tmp_path).notify("대기 알림")
    result = runner.invoke(app, [*args, "doctor"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["pending_notifications"] == 1
    assert report["heartbeat_age_seconds"] is None
    assert report["alert_file_entries"] == 0


def test_status_reports_alert_file_entries(tmp_path):
    from shorts_local.cli import app
    from shorts_local.health import append_alert

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    append_alert(tmp_path, "보관된 알림")
    result = runner.invoke(app, [*args, "status"])
    assert result.exit_code == 0, result.output
    assert "알림 파일: 1건" in result.output


def test_status_lists_instagram_source_health(tmp_path):
    from shorts_local.cli import app
    from shorts_local.instagram_store import InstagramStore
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    store = Store(tmp_path)
    store.update_profile_settings("default", {"production_type": "instagram"})
    InstagramStore(store).add_source("pets", now=100)
    result = runner.invoke(app, [*args, "status"])
    assert result.exit_code == 0, result.output
    assert "@pets" in result.output
    assert "연속 실패: 0" in result.output
    assert "마지막 성공: -" in result.output


def test_doctor_reports_channel_scoped_auth_wait(tmp_path):
    import json

    from shorts_local.cli import app
    from shorts_local.store import Store

    runner = CliRunner()
    args = ["--home", str(tmp_path)]
    runner.invoke(app, [*args, "init"])
    Store(tmp_path).set_value("instagram_auth_wait_channels", ["animals"])
    result = runner.invoke(app, [*args, "doctor"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["instagram"]["auth_wait"] is True
    assert report["instagram"]["auth_wait_channels"] == ["animals"]
