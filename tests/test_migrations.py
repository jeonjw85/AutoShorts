import sqlite3

import pytest

from shorts_local.process import BlockedError, BusyError, exclusive_lock
from shorts_local.store import Store


def test_legacy_migration_preserves_jobs_and_files(legacy_home):
    s = Store(legacy_home)
    assert s.profile().settings.topic == "보존할 기존 주제"
    ready, uploading = s.get("legacy-ready"), s.get("legacy-upload")
    assert ready["channel_id"] == uploading["channel_id"] == "default"
    assert ready["status"] == "ready" and uploading["status"] == "retry"
    assert ready["settings"]["topic"] == "작업 당시 주제"
    assert uploading["dedup_key"] == "schedule:2020-01-01"
    assert uploading["attempts"] == 1
    assert ready["legacy_upload"] == 0 and uploading["legacy_upload"] == 1
    assert s.value("paused") and s.value("telegram_offset") == 101
    assert s.telegram_reply(100) == {"text": "접수"}
    assert s.pending_notifications()[0]["text"] == "완성"
    assert (legacy_home / "jobs/legacy-ready/video.mp4").read_bytes() == b"unchanged-video"
    auth = legacy_home / "channels/default/auth/youtube.json"
    assert auth.read_bytes() == (legacy_home / "auth/youtube.json").read_bytes()
    assert auth.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(legacy_home / "backups/pre-channels.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    assert len(Store(legacy_home).list()) == 2


def test_migration_resumes_after_auth_copy_failure(legacy_home, monkeypatch):
    import shorts_local.migrations as migrations

    original = migrations._copy_legacy_auth

    def interrupted(root):
        original(root)
        raise RuntimeError("interrupted after copying")

    monkeypatch.setattr(migrations, "_copy_legacy_auth", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        Store(legacy_home)
    with sqlite3.connect(legacy_home / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    before = (legacy_home / "backups/pre-channels.sqlite3").read_bytes()
    monkeypatch.setattr(migrations, "_copy_legacy_auth", original)
    s = Store(legacy_home)
    assert len(s.profiles()) == 1 and len(s.list()) == 2
    assert (legacy_home / "backups/pre-channels.sqlite3").read_bytes() == before


@pytest.mark.parametrize("lock", ["service.lock", "worker.lock"])
def test_migration_respects_running_locks_and_existing_auth(legacy_home, lock):
    with exclusive_lock(legacy_home / lock), pytest.raises(BusyError):
        Store(legacy_home)
    destination = legacy_home / "channels/default/auth/youtube.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"different":"credential"}')
    with pytest.raises(BlockedError):
        Store(legacy_home)
    assert destination.read_text() == '{"different":"credential"}'
    destination.unlink()
    Store(legacy_home)
    with exclusive_lock(legacy_home / lock):
        assert len(Store(legacy_home).list()) == 2


def test_migration_handles_older_optional_columns(legacy_home):
    with sqlite3.connect(legacy_home / "state.sqlite3") as db:
        db.execute("ALTER TABLE jobs DROP COLUMN manual_run")
        db.execute("ALTER TABLE notifications DROP COLUMN video")
    s = Store(legacy_home)
    assert s.get("legacy-ready")["manual_run"] == 0
    assert s.pending_notifications()[0]["video"] is None
    assert len(Store(legacy_home).list()) == 2
