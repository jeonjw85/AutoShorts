import time

import pytest


def _job(tmp_path, *, status="ready", days_old=30, upload_state=False, validation=True):
    from shorts_local.config import Settings
    from shorts_local.process import atomic_json
    from shorts_local.store import Store

    store = Store(tmp_path)
    job = store.enqueue(Settings())
    folder = tmp_path / "jobs" / job
    for name in ("voice", "images", "art", "segments"):
        (folder / name).mkdir(parents=True, exist_ok=True)
        (folder / name / "part.bin").write_bytes(b"x")
    for name in ("video.mp4", "story.json", "timeline.json", "captions.srt", "cover.png"):
        (folder / name).write_bytes(b"x")
    (folder / "plan.log").write_text("log")
    if validation:
        atomic_json(folder / "validation.json", {"duration": 1})
    if upload_state:
        atomic_json(folder / "upload-state.json", {"url": "u"})
    store.update(job, status=status)
    with store.db() as db:
        db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - days_old * 86400, job))
    return store, job, folder


def test_cleanup_plans_only_transient_artifacts(tmp_path):
    from shorts_local.cleanup import plan_cleanup

    store, job, folder = _job(tmp_path)
    names = {path.name for path in plan_cleanup(store)}
    assert names == {"voice", "images", "art", "segments", "plan.log"}
    assert (folder / "video.mp4").exists()


@pytest.mark.parametrize("kwargs", [
    {"days_old": 1}, {"status": "failed"}, {"upload_state": True}, {"validation": False},
])
def test_cleanup_skips_protected_jobs(tmp_path, kwargs):
    from shorts_local.cleanup import plan_cleanup

    store, job, folder = _job(tmp_path, **kwargs)
    assert plan_cleanup(store) == []


def test_apply_cleanup_removes_only_planned_paths(tmp_path):
    from shorts_local.cleanup import apply_cleanup, plan_cleanup

    store, job, folder = _job(tmp_path)
    assert apply_cleanup(plan_cleanup(store)) == 5
    assert not (folder / "voice").exists() and not (folder / "plan.log").exists()
    for name in ("video.mp4", "story.json", "validation.json", "timeline.json", "captions.srt"):
        assert (folder / name).exists()


def test_cleanup_skips_corrupt_validation_record(tmp_path):
    from shorts_local.cleanup import plan_cleanup

    store, job, folder = _job(tmp_path)
    (folder / "validation.json").write_text("{broken")
    assert plan_cleanup(store) == []


@pytest.mark.parametrize("name", ["draft.txt", "request.json", "exact-timing-probe.mp4"])
def test_cleanup_removes_transient_files_but_keeps_uploads(tmp_path, name):
    from shorts_local.cleanup import apply_cleanup, plan_cleanup

    store, job, folder = _job(tmp_path)
    (folder / name).write_bytes(b"x")
    assert folder / name in plan_cleanup(store)
    apply_cleanup(plan_cleanup(store))
    assert not (folder / name).exists()
    for kept in ("video.mp4", "story.json", "validation.json"):
        assert (folder / kept).exists()


def test_apply_cleanup_reports_failures_and_keeps_going(tmp_path, monkeypatch):
    import shutil as module

    from shorts_local.cleanup import apply_cleanup

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    original = module.rmtree

    def flaky(path, *args, **kwargs):
        if str(path).endswith("one"):
            raise OSError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "rmtree", flaky)
    failures = []
    assert apply_cleanup([one, two], failures=failures) == 1
    assert one.exists() and not two.exists()
    assert failures and failures[0][0] == one


def test_cli_rejects_negative_days(tmp_path):
    from typer.testing import CliRunner

    from shorts_local.cli import app

    result = CliRunner().invoke(app, ["--home", str(tmp_path), "cleanup", "--days", "-1"])
    assert result.exit_code != 0
