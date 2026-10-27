"""Service liveness files. No imports from the store or network layers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .process import atomic_json

if TYPE_CHECKING:
    from .store import Store

HEARTBEAT_FILE = "heartbeat.json"
ALERTS_FILE = "alerts.log"
SERVICE_LOG = "service.log"
STALE_SECONDS = 300
BEAT_INTERVAL_SECONDS = 15
LOG_LIMIT_BYTES = 20 * 1024**2
MAX_NOTIFICATION_ATTEMPTS = 5
logger = logging.getLogger(__name__)


def write_heartbeat(root: Path, *, version: str, pid: int, started_at: float,
                    last_job_id: str | None, queue_depth: int,
                    now: float | None = None) -> dict:
    payload = {
        "pid": pid,
        "version": version,
        "started_at": started_at,
        "updated_at": time.time() if now is None else now,
        "last_job_id": last_job_id,
        "queue_depth": queue_depth,
    }
    atomic_json(root / HEARTBEAT_FILE, payload)
    return payload


def read_heartbeat(root: Path) -> dict | None:
    try:
        data = json.loads((root / HEARTBEAT_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_label(age: float | None) -> str:
    if age is None:
        return "기록 없음"
    minutes = int(age // 60)
    label = f"{minutes}분 전" if minutes else "방금"
    return f"{label} (중단 의심)" if age > STALE_SECONDS else label


def heartbeat_age(root: Path, *, now: float | None = None) -> float | None:
    data = read_heartbeat(root)
    updated = data.get("updated_at") if data else None
    if not isinstance(updated, (int, float)) or isinstance(updated, bool):
        return None
    return max(0.0, (time.time() if now is None else now) - updated)


def rotate_log(root: Path, *, limit: int = LOG_LIMIT_BYTES) -> bool:
    """Copy-then-truncate: launchd holds the file descriptor, so it must stay valid."""
    path = root / SERVICE_LOG
    try:
        if path.stat().st_size <= limit:
            return False
    except OSError:
        return False
    try:
        with path.open("rb") as source, (root / f"{SERVICE_LOG}.1").open("wb") as sink:
            shutil.copyfileobj(source, sink)
        with path.open("r+b") as stream:
            stream.truncate(0)
    except OSError:
        return False
    return True


def append_alert(root: Path, text: str, *, job_id: str | None = None,
                 now: float | None = None) -> None:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, UTC).isoformat()
    line = f"{stamp}\t{job_id or '-'}\t{' '.join(text.split())[:3800]}\n"
    path = root / ALERTS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(line)


def drain_notifications(store: Store, root: Path, *, limit: int = 100) -> int:
    moved, failed = 0, 0
    for note in store.notifications(limit):
        try:
            append_alert(root, note["text"], job_id=note["job_id"])
        except OSError:
            failed += 1
            continue
        store.discard_notification(note["id"])
        moved += 1
    if failed:
        logger.error("알림 파일을 쓸 수 없어 %d건을 대기열에 유지합니다", failed)
    return moved


def alerts_count(root: Path) -> int:
    try:
        with (root / ALERTS_FILE).open("r", encoding="utf-8", errors="replace") as stream:
            return sum(1 for _ in stream)
    except OSError:
        return 0
