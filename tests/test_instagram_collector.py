import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shorts_local.config import Settings
from shorts_local.instagram import InstagramError, fetch_source
from shorts_local.instagram_models import InstagramInput
from shorts_local.instagram_store import InstagramStore
from shorts_local.process import atomic_json, exclusive_lock
from shorts_local.store import Store
from tests.instagram_helpers import batch, post
from tests.test_service import update


def checkpoint(request, folder, *, error=None, complete=True):
    value = batch(request["source"], [post()], run_id=request["run_id"], complete=complete,
                  cursor=None if complete else {"page": 2}, error_code=error)
    atomic_json(folder / "result.json", value.model_dump())


def test_checkpoint_survives_parent_death_before_apply(tmp_path, monkeypatch):
    import shorts_local.collector as collector

    s = Store(tmp_path)
    ig = InstagramStore(s)
    source = ig.add_source("pets", now=100)

    def child(args, log, timeout):
        folder = log.parent
        checkpoint(json.loads((folder / "request.json").read_text()), folder)

    monkeypatch.setattr(collector, "run_process", child)
    original = InstagramStore.apply_scan
    monkeypatch.setattr(InstagramStore, "apply_scan", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        collector.run_collection(tmp_path, now=500)
    assert ig.materials() == []
    monkeypatch.setattr(InstagramStore, "apply_scan", original)
    assert collector.run_collection(tmp_path, now=500) == source["id"]
    assert len(ig.materials()) == 1 and ig.source(source["id"])["scan_revision"] == 1
    assert collector.run_collection(tmp_path, now=501) is None
    assert len(ig.materials()) == 1


def test_partial_scan_failure_preserves_watermark(tmp_path, monkeypatch):
    import shorts_local.collector as collector

    ig = InstagramStore(Store(tmp_path))
    source = ig.add_source("pets", now=100)

    def child(args, log, timeout):
        checkpoint(json.loads((log.parent / "request.json").read_text()), log.parent,
                   error="transient", complete=False)
        raise RuntimeError("child timeout")

    monkeypatch.setattr(collector, "run_process", child)
    collector.run_collection(tmp_path, now=500)
    after = ig.source(source["id"])
    assert len(ig.materials()) == 1 and after["cursor"] == {"page": 2}
    assert after["completed_through"] is None and after["next_run"] == 800
    assert after["failure_count"] == 1


def test_session_generation_recovers_only_auth_wait_jobs(tmp_path, monkeypatch):
    s = Store(tmp_path)
    ig = InstagramStore(s)
    job = ig.import_video(post().url)
    s.claim()
    ig.wait_job(job, "auth_wait")
    other = s.enqueue()
    s.update(other, status="blocked", error="unrelated")
    atomic_json(tmp_path / "auth/instagram.json", {
        "username": "pets", "session": {"sessionid": "fixture", "csrftoken": "fixture"},
        "generation": "a" * 32,
    })
    original = s.set_value

    def interrupted(key, value):
        if key == "instagram_auth_generation":
            raise RuntimeError("DB commit interrupted")
        original(key, value)

    monkeypatch.setattr(s, "set_value", interrupted)
    with pytest.raises(RuntimeError):
        ig.reconcile_auth()
    assert s.get(job)["status"] == "auth_wait"
    monkeypatch.setattr(s, "set_value", original)
    ig.reconcile_auth()
    assert s.get(job)["status"] == "queued" and s.get(job)["attempts"] == 0
    assert s.get(job)["manual_run"] == 0
    assert s.get(other)["status"] == "blocked"
    assert not s.value("instagram_auth_wait")


def test_collector_and_fetch_share_lock_without_spending_attempts(tmp_path):
    s = Store(tmp_path)
    ig = InstagramStore(s)
    job = ig.import_video(post().url)
    claimed = s.claim()
    with exclusive_lock(tmp_path / "instagram.lock"), pytest.raises(InstagramError) as exc:
        fetch_source(tmp_path, InstagramInput.model_validate(claimed["input"]),
                     tmp_path / "jobs" / job, Settings())
    ig.wait_job(job, exc.value.code, retry_at=exc.value.retry_at)
    assert s.get(job)["status"] == "retry" and s.get(job)["attempts"] == 0
    next_job = s.enqueue()
    assert s.claim()["id"] == next_job


def test_backoff_and_recovery_notifications(tmp_path):
    s = Store(tmp_path)
    ig = InstagramStore(s)
    source = ig.add_source("pets", now=100)
    for expected in (300, 600, 1200, 2400, 4800, 9600, 19200, 21600, 21600):
        ig.record_failure(source["id"], "transient", now=500)
        assert ig.source(source["id"])["next_run"] == 500 + expected
    assert len(s.pending_notifications()) == 1
    ig.apply_scan(batch(ig.source(source["id"]), [post()]), now=500)
    ig.record_failure(source["id"], "transient", now=500)
    assert len(s.pending_notifications()) == 2
    ig.record_failure(source["id"], "rate_limit", now=500)
    assert s.value("instagram_next_run") == 4100
    ig.request_scan("default", now=501)
    assert ig.due_source(now=501) is None


def test_new_login_is_not_overwritten_by_old_auth_error(tmp_path, monkeypatch):
    import shorts_local.collector as collector

    s = Store(tmp_path)
    ig = InstagramStore(s)
    source = ig.add_source("pets", now=100)

    def child(args, log, timeout):
        checkpoint(json.loads((log.parent / "request.json").read_text()), log.parent,
                   error="auth_wait", complete=False)
        atomic_json(tmp_path / "auth/instagram.json", {
            "username": "pets", "session": {"sessionid": "new"}, "generation": "b" * 32,
        })
        ig.reconcile_auth()

    monkeypatch.setattr(collector, "run_process", child)
    collector.run_collection(tmp_path, now=500)
    assert not s.value("instagram_auth_wait", False)
    assert ig.source(source["id"])["status"] != "auth_wait"


def test_slow_collector_keeps_bot_responsive(tmp_path, monkeypatch):
    import shorts_local.service as service

    s = Store(tmp_path)
    s.update_profile_settings("default", {"schedule_enabled": False})
    job = s.enqueue()
    entered, release, finished, stop = (threading.Event() for _ in range(4))
    calls = []

    def collect(root):
        calls.append("collect")
        entered.set()
        assert release.wait(5)

    def work(root):
        Store(root).update(job, status="ready")
        finished.set()

    class Bot:
        user_id = 42

        def poll(self, offset):
            assert entered.wait(5) and finished.wait(5)
            return [update("/pause", update_id=808)]

        def send(self, reply):
            release.set()
            stop.set()

        def close(self):
            release.set()

    monkeypatch.setattr(service, "credentials", lambda root: ("fixture", 42))
    monkeypatch.setattr(service, "Telegram", lambda *args: Bot())
    monkeypatch.setattr(service, "run_collection", collect)
    monkeypatch.setattr(service, "run_next", work)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.serve, tmp_path, stop)
        future.result(timeout=8)
    assert s.profile().paused and s.value("telegram_offset") == 809
    assert s.get(job)["status"] == "ready" and calls == ["collect"]
