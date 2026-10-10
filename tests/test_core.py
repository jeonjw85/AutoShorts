from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError


def test_no_paid_provider_or_budget_can_be_configured():
    from shorts_local.config import Settings

    for change in ({"planner": "api"}, {"monthly_budget": 1}, {"fps": 0}):
        with pytest.raises(ValidationError):
            Settings(**change)


@pytest.mark.parametrize("change", [
    {"schedule_time": "25:00"}, {"timezone": "Nowhere/Mars"},
    {"width": 721}, {"height": 100}, {"topic": " "},
])
def test_invalid_settings_rejected(change):
    from shorts_local.config import Settings

    with pytest.raises(ValidationError):
        Settings(**change)


def test_config_roundtrip_and_private_permissions(tmp_path):
    from shorts_local.config import Settings

    path = tmp_path / "config.json"
    Settings(topic="우주 우체부의 하루").save(path)
    assert Settings.load(path).topic == "우주 우체부의 하루"
    assert path.stat().st_mode & 0o777 == 0o600


def test_queue_deduplicates_and_snapshots_configuration(tmp_path):
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    settings = Settings(topic="첫 주제")
    first = store.enqueue(settings, key="scheduled:2026-10-10", publish=False)
    settings.topic = "다음 주제"
    assert store.enqueue(settings, key="scheduled:2026-10-10", publish=False) == first
    assert Store(tmp_path).get(first)["settings"]["topic"] == "첫 주제"


def test_only_one_worker_can_claim_a_job(tmp_path):
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    job = store.enqueue(Settings(), publish=False)
    with ThreadPoolExecutor(8) as pool:
        claimed = list(pool.map(lambda _: Store(tmp_path).claim(), range(8)))
    assert [x["id"] for x in claimed if x] == [job]
    assert store.get(job)["attempts"] == 1


def test_recovery_preserves_stage_and_blocks_uncertain_duplicate_upload(tmp_path):
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    job = store.enqueue(Settings(), publish=True)
    store.claim()
    store.update(job, stage="upload")
    store.recover()
    assert store.get(job)["status"] == "queued"
    assert store.get(job)["stage"] == "upload"
    store.update(job, status="blocked", error="upload outcome unknown")
    assert store.claim() is None


def test_auto_retries_end_at_three_attempts(tmp_path):
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    job = store.enqueue(Settings(), publish=False)
    for attempt in range(3):
        assert store.claim(now=1000 + attempt)["id"] == job
        store.fail(job, "temporary", delay=0, now=1000 + attempt)
    assert store.claim(now=2000) is None
    assert store.get(job)["status"] == "failed"


def test_process_lock_excludes_another_worker(tmp_path):
    from shorts_local.process import BusyError, exclusive_lock

    with exclusive_lock(tmp_path / "worker.lock"):
        with pytest.raises(BusyError), exclusive_lock(tmp_path / "worker.lock"):
            pass
