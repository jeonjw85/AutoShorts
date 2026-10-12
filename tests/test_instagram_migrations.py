import sqlite3

import pytest

from shorts_local.process import BlockedError, BusyError, exclusive_lock
from shorts_local.store import Store


def snapshot(root):
    with sqlite3.connect(root / "state.sqlite3") as db:
        return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                for name in ("channels", "jobs", "state", "notifications", "telegram_updates")}


def test_schema1_upgrade_preserves_bytes_and_history(schema1_home):
    before = snapshot(schema1_home)
    backup = (schema1_home / "backups/pre-channels.sqlite3").read_bytes()
    video = (schema1_home / "jobs/legacy-ready/video.mp4").read_bytes()
    s = Store(schema1_home)
    with s.db() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT input FROM jobs").fetchall()[0][0] is None
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    after = snapshot(schema1_home)
    assert [row[:-1] for row in after.pop("jobs")] == before.pop("jobs")
    assert after == before
    assert (schema1_home / "backups/pre-channels.sqlite3").read_bytes() == backup
    assert (schema1_home / "jobs/legacy-ready/video.mp4").read_bytes() == video
    path = schema1_home / "backups/pre-instagram.sqlite3"
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    saved = path.read_bytes()
    Store(schema1_home)
    assert path.read_bytes() == saved


@pytest.mark.parametrize("boundary", ["backup", "ddl"])
def test_upgrade_restarts_after_backup_or_ddl_failure(schema1_home, monkeypatch, boundary):
    import shorts_local.migrations as migration

    original = getattr(migration, "_backup" if boundary == "backup" else "_migrate_instagram")

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected migration stop")

    name = "_backup" if boundary == "backup" else "_migrate_instagram"
    before = snapshot(schema1_home)
    monkeypatch.setattr(migration, name, interrupted)
    with pytest.raises(RuntimeError, match="injected"):
        Store(schema1_home)
    assert snapshot(schema1_home) == before
    with sqlite3.connect(schema1_home / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    monkeypatch.setattr(migration, name, original)
    assert len(Store(schema1_home).list()) == 2


@pytest.mark.parametrize("lock", ["service.lock", "worker.lock"])
def test_schema2_locking_and_future_versions(schema1_home, lock):
    with exclusive_lock(schema1_home / lock), pytest.raises(BusyError):
        Store(schema1_home)
    Store(schema1_home)
    with exclusive_lock(schema1_home / lock):
        assert len(Store(schema1_home).profiles()) == 2
    with sqlite3.connect(schema1_home / "state.sqlite3") as db:
        db.execute("PRAGMA user_version=4")
    with pytest.raises(BlockedError):
        Store(schema1_home)


@pytest.mark.parametrize("legacy", [False, True])
def test_new_and_schema0_homes_reach_current_schema(tmp_path, legacy_home, legacy):
    root = legacy_home if legacy else tmp_path / "fresh"
    with Store(root).db() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
