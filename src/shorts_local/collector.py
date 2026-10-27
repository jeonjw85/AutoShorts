"""A single bounded metadata collector, independent of the heavy AI worker."""

import json
import sys
import time
import uuid
from pathlib import Path

from .instagram import InstagramError, check_wait, session_generation
from .instagram_models import ScanBatch
from .instagram_store import InstagramStore
from .process import BusyError, atomic_json, exclusive_lock, run_process
from .store import Store


def _apply_checkpoint(ig: InstagramStore, folder: Path, now: float, *, interrupted=False) -> str | None:
    try:
        request = json.loads((folder / "request.json").read_text())
        batch = ScanBatch.model_validate_json((folder / "result.json").read_text())
        if (batch.run_id != request["run_id"] or batch.source_id != request["source"]["id"]
                or batch.base_revision != request["source"]["scan_revision"]):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if interrupted and not batch.complete and not batch.error_code:
        batch.error_code = "transient"
    with ig.store.transaction():
        if not ig.apply_scan(batch, now=now):
            return None
        if batch.error_code:
            ig.record_failure(batch.source_id, batch.error_code, retry_at=batch.retry_at,
                              now=now, auth_generation=request.get("auth_generation"))
    return batch.source_id


def run_collection(root: Path, *, channel_id: str | None = None, now: float | None = None) -> str | None:
    store = Store(root)
    ig = InstagramStore(store)
    now = time.time() if now is None else now
    try:
        with exclusive_lock(root / "collector.lock"):
            ig.reconcile_auth()
            for path in sorted((root / "collect").glob("*/request.json")):
                if recovered := _apply_checkpoint(ig, path.parent, now):
                    return recovered
            source = ig.due_source(channel_id=channel_id, now=now)
            if source is None:
                return None
            folder = root / "collect" / source["id"]
            try:
                with exclusive_lock(root / "instagram.lock"):
                    check_wait(store, source["channel_id"])
                    # A pause may have happened while selecting/waiting for the API slot.
                    current = ig.due_source(channel_id=channel_id, now=now)
                    if current is None:
                        return None
                    source = current
                    folder = root / "collect" / source["id"]
                    atomic_json(folder / "request.json", {
                        "run_id": uuid.uuid4().hex, "source": source,
                        "auth_generation": session_generation(root),
                    })
                    (folder / "result.json").unlink(missing_ok=True)
                    interrupted = False
                    try:
                        run_process([sys.executable, "-m", "shorts_local.instagram", "scan",
                                     str(root), str(folder)], folder / "scan.log", 120)
                    except RuntimeError:
                        interrupted = True
                if not _apply_checkpoint(ig, folder, now, interrupted=interrupted):
                    ig.record_failure(source["id"], "transient", now=now)
            except BusyError:
                ig.record_failure(source["id"], "busy", now=now)
            except InstagramError as exc:
                ig.record_failure(source["id"], exc.code, retry_at=exc.retry_at, now=now)
            return source["id"]
    except BusyError:
        return None
