"""One feed at a time, with a hard subprocess deadline and no command transaction."""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .news_store import NewsStore
from .process import BusyError, atomic_json, exclusive_lock, run_process
from .store import Store


def _fetch_feed(source: dict, folder: Path) -> dict:
    atomic_json(folder / "request.json", source)
    run_process([sys.executable, "-m", "shorts_local.news_feed", str(folder)], folder / "scan.log", 120)
    result = json.loads((folder / "result.json").read_text())
    if "error" in result:
        raise ValueError("피드 조회 실패")
    return result


def run_news_collection(root: Path, *, channel_id: str | None = None, now: float | None = None) -> str | None:
    store = Store(root)
    news = NewsStore(store)
    started_at = time.time() if now is None else now
    try:
        with exclusive_lock(root / "news-collector.lock"):
            if shutil.disk_usage(root).free < 5 * 1024**3:
                return None
            source = news.due_source(channel_id=channel_id, now=started_at)
            if source is None:
                return None
            try:
                with tempfile.TemporaryDirectory(prefix="news-scan-", dir=root) as directory:
                    result = _fetch_feed(source, Path(directory))
                news.record_success(source["id"], result, now=time.time() if now is None else now)
            except Exception:
                news.record_failure(source["id"], now=time.time() if now is None else now)
            return source["id"]
    except BusyError:
        return None
