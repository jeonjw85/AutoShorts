def test_heartbeat_records_activity_and_reports_age(tmp_path):
    from shorts_local.health import heartbeat_age, read_heartbeat, write_heartbeat

    write_heartbeat(tmp_path, version="0.1.3", pid=7, started_at=100.0,
                    last_job_id="abc", queue_depth=2, now=1000.0)
    data = read_heartbeat(tmp_path)
    assert data["pid"] == 7 and data["version"] == "0.1.3"
    assert data["started_at"] == 100.0 and data["last_job_id"] == "abc"
    assert data["queue_depth"] == 2 and data["updated_at"] == 1000.0
    assert heartbeat_age(tmp_path, now=1300.0) == 300.0


def test_missing_or_corrupt_heartbeat_is_not_an_error(tmp_path):
    from shorts_local.health import heartbeat_age, read_heartbeat

    assert read_heartbeat(tmp_path) is None
    assert heartbeat_age(tmp_path) is None
    (tmp_path / "heartbeat.json").write_text("{broken")
    assert read_heartbeat(tmp_path) is None
    assert heartbeat_age(tmp_path) is None


def test_rotate_log_keeps_service_log_in_place(tmp_path):
    from shorts_local.health import rotate_log

    (tmp_path / "service.log").write_text("x" * 11)
    (tmp_path / "service.log.1").write_text("old")
    assert rotate_log(tmp_path, limit=10) is True
    assert (tmp_path / "service.log.1").read_text() == "x" * 11
    assert (tmp_path / "service.log").read_text() == ""
    (tmp_path / "service.log").write_text("y" * 11)
    assert rotate_log(tmp_path, limit=10) is True
    assert (tmp_path / "service.log.1").read_text() == "y" * 11
    assert rotate_log(tmp_path, limit=10) is False


def test_alert_file_is_private_and_one_line_per_alert(tmp_path):
    from shorts_local.health import append_alert

    append_alert(tmp_path, "첫 줄\n둘째 줄", job_id="abc", now=0)
    path = tmp_path / "alerts.log"
    text = path.read_text()
    assert text.count("\n") == 1
    assert "abc" in text and "첫 줄 둘째 줄" in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_notification_stops_after_five_failures(tmp_path):
    from shorts_local.health import MAX_NOTIFICATION_ATTEMPTS
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("실패 알림")
    note = store.pending_notifications()[0]
    for _ in range(MAX_NOTIFICATION_ATTEMPTS - 1):
        assert store.notification_result(note["id"], False) is True
    assert store.notification_result(note["id"], False) is False
    assert store.notification_count() == 0
    assert "실패 알림" in (tmp_path / "alerts.log").read_text()


def test_notification_count_and_queue_depth(tmp_path):
    from shorts_local.config import Settings
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("하나")
    job = store.enqueue(Settings())
    assert store.notification_count() == 1
    assert store.queue_depth() == 1
    store.update(job, status="ready")
    assert store.queue_depth() == 0
    assert [n["text"] for n in store.notifications()] == ["하나"]


def test_drain_moves_pending_notifications_to_file(tmp_path):
    from shorts_local.health import drain_notifications
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("하나")
    store.notify("둘")
    assert drain_notifications(store, tmp_path) == 2
    assert store.notification_count() == 0
    log = (tmp_path / "alerts.log").read_text()
    assert "하나" in log and "둘" in log


def test_drain_keeps_notification_when_alert_file_is_unwritable(tmp_path):
    from shorts_local.health import drain_notifications
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("하나")
    (tmp_path / "alerts.log").mkdir()
    assert drain_notifications(store, tmp_path) == 0
    assert store.notification_count() == 1


def test_notification_is_discarded_when_alert_file_stays_unwritable(tmp_path):
    from shorts_local.health import MAX_NOTIFICATION_ATTEMPTS
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("하나")
    note = store.pending_notifications()[0]
    (tmp_path / "alerts.log").mkdir()
    for _ in range(MAX_NOTIFICATION_ATTEMPTS - 1):
        assert store.notification_result(note["id"], False) is True
    assert store.notification_result(note["id"], False) is False
    assert store.notification_count() == 0


def test_drain_never_discards_alerts_when_the_file_stays_unwritable(tmp_path):
    from shorts_local.health import MAX_NOTIFICATION_ATTEMPTS, drain_notifications
    from shorts_local.store import Store

    store = Store(tmp_path)
    store.notify("하나")
    (tmp_path / "alerts.log").mkdir()
    for _ in range(MAX_NOTIFICATION_ATTEMPTS * 2):
        assert drain_notifications(store, tmp_path) == 0
    assert store.notification_count() == 1
