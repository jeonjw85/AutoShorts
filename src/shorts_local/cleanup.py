"""Remove transient job artifacts while final videos and recovery records stay."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .store import Store

TRANSIENT_DIRS = ("voice", "images", "art", "segments")
TRANSIENT_FILES = ("draft.txt", "request.json", "exact-timing-probe.mp4")
TRANSIENT_PATTERNS = ("*.log",)
FINAL_STATUSES = {"uploaded", "ready"}


def _has_validation(folder: Path) -> bool:
    try:
        data = json.loads((folder / "validation.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data)


def plan_cleanup(store: Store, *, days: int = 14, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    planned: list[Path] = []
    for job in store.list(limit=100_000):
        if job["status"] not in FINAL_STATUSES or job["updated"] > cutoff:
            continue
        folder = store.root / "jobs" / job["id"]
        if not folder.is_dir() or not _has_validation(folder):
            continue
        if (folder / "upload-state.json").exists():
            continue
        for name in TRANSIENT_DIRS:
            path = folder / name
            if path.is_dir():
                planned.append(path)
        for name in TRANSIENT_FILES:
            path = folder / name
            if path.is_file():
                planned.append(path)
        for pattern in TRANSIENT_PATTERNS:
            planned.extend(path for path in sorted(folder.glob(pattern)) if path.is_file())
    return planned


def apply_cleanup(paths: list[Path], *, failures: list[tuple[Path, str]] | None = None) -> int:
    removed = 0
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            if failures is not None:
                failures.append((path, str(exc)))
            continue
        removed += 1
    return removed
