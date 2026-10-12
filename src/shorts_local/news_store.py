"""Channel-scoped article sources and atomic job reservations."""

import time
import uuid

from .config import Settings
from .news_models import NewsInput, canonicalize_article_url
from .store import Store


class NewsStore:
    def __init__(self, store: Store):
        self.store = store

    def source(self, source_id: str) -> dict:
        with self.store.db() as db:
            row = db.execute("SELECT * FROM news_sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise ValueError("뉴스 수집원 ID를 찾을 수 없습니다.")
        return dict(row)

    def sources(self, channel_id: str = "default") -> list[dict]:
        self.store.profile(channel_id)
        with self.store.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM news_sources
                WHERE channel_id=? ORDER BY registered_at,id""", (channel_id,))]

    def add_source(self, feed_url: str, *, channel_id: str = "default", now=None) -> dict:
        url = canonicalize_article_url(feed_url)
        now = time.time() if now is None else now
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            db.execute("""INSERT OR IGNORE INTO news_sources
                (id,channel_id,feed_url,registered_at) VALUES (?,?,?,?)""",
                       (uuid.uuid4().hex[:12], channel_id, url, now))
            return dict(db.execute("SELECT * FROM news_sources WHERE channel_id=? AND feed_url=?",
                                   (channel_id, url)).fetchone())

    def materials(self, channel_id: str = "default", limit: int = 20) -> list[dict]:
        self.store.profile(channel_id)
        with self.store.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM news_items
                WHERE channel_id=? ORDER BY discovered_at DESC,id LIMIT ?""", (channel_id, limit))]

    def _existing_key(self, channel_id, key):
        with self.store.db() as db:
            row = db.execute("SELECT id FROM jobs WHERE channel_id=? AND dedup_key=?",
                             (channel_id, key)).fetchone()
            return row[0] if row else None

    def _reserve(self, item, publish, key):
        if item["job_id"]:
            return item["job_id"]
        settings = self.store.profile(item["channel_id"]).settings
        settings = Settings.model_validate(settings.model_dump() | {"production_type": "news"})
        data = NewsInput(item_id=item["id"], source_id=item["source_id"],
                         url=item["canonical_url"], published_at=item["published_at"])
        return self.store.enqueue(settings, key, publish, channel_id=item["channel_id"], input=data)

    def import_article(self, url: str, *, channel_id="default", publish=None, key=None) -> str:
        url = canonicalize_article_url(url)
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            if existing := self._existing_key(channel_id, key):
                return existing
            db.execute("""INSERT OR IGNORE INTO news_items
                (id,channel_id,canonical_url,discovered_at,status) VALUES (?,?,?,?,'available')""",
                       (uuid.uuid4().hex[:12], channel_id, url, time.time()))
            item = db.execute("SELECT * FROM news_items WHERE channel_id=? AND canonical_url=?",
                              (channel_id, url)).fetchone()
            return self._reserve(item, publish, key)

    def apply_feed(self, source_id: str, entries: list[dict], *, now: float) -> int:
        added = 0
        with self.store.transaction(), self.store.db() as db:
            source = self.source(source_id)
            for entry in entries[:200]:
                try:
                    data = NewsInput(item_id=uuid.uuid4().hex[:12], source_id=source_id,
                                     url=entry["url"], published_at=entry.get("published_at"))
                except (ValueError, KeyError):
                    continue
                if data.published_at is None or not source["registered_at"] <= data.published_at <= now:
                    continue
                added += db.execute("""INSERT OR IGNORE INTO news_items
                    (id,channel_id,source_id,canonical_url,title,published_at,discovered_at,status)
                    VALUES (?,?,?,?,?,?,?,'available')""",
                    (data.item_id, source["channel_id"], source_id, data.url,
                     str(entry.get("title", ""))[:500], data.published_at, now)).rowcount
        return added

    def enqueue_next(self, *, channel_id="default", key=None, publish=None) -> str | None:
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            if existing := self._existing_key(channel_id, key):
                return existing
            item = db.execute("""SELECT i.* FROM news_items i
                JOIN news_sources s ON s.id=i.source_id
                WHERE i.channel_id=? AND i.status='available' AND i.job_id IS NULL AND s.active=1
                ORDER BY i.published_at,i.canonical_url LIMIT 1""", (channel_id,)).fetchone()
            return self._reserve(item, publish, key) if item else None

    def set_source_active(self, source_id: str, active: bool) -> None:
        with self.store.transaction(), self.store.db() as db:
            self.source(source_id)
            db.execute("UPDATE news_sources SET active=?,next_run=CASE WHEN ? THEN 0 ELSE next_run END WHERE id=?",
                       (active, active, source_id))

    def request_scan(self, channel_id: str) -> None:
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            # A manual request does not bypass an ongoing failure's backoff.
            db.execute("UPDATE news_sources SET next_run=0 WHERE channel_id=? AND active=1 AND failure_count=0",
                       (channel_id,))

    def due_source(self, *, channel_id=None, now=None) -> dict | None:
        now = time.time() if now is None else now
        if self.store.value("paused", False):
            return None
        with self.store.db() as db:
            row = db.execute("""SELECT s.* FROM news_sources s JOIN channels c ON c.id=s.channel_id
                WHERE s.active=1 AND c.paused=0 AND s.next_run<=?
                AND (? IS NULL OR s.channel_id=?) ORDER BY s.next_run,s.registered_at,s.id LIMIT 1""",
                (now, channel_id, channel_id)).fetchone()
        return dict(row) if row else None

    def record_success(self, source_id: str, result: dict, *, now: float) -> None:
        with self.store.transaction(), self.store.db() as db:
            source = self.source(source_id)
            self.apply_feed(source_id, result["entries"], now=now)
            interval = self.store.profile(source["channel_id"]).settings.news_poll_minutes * 60
            deferred = any(isinstance(entry.get("published_at"), (int, float))
                           and entry["published_at"] > now for entry in result["entries"])
            # A 304 must not hide an entry that becomes eligible on a later poll.
            etag = None if deferred else result.get("etag")
            modified = None if deferred else result.get("last_modified")
            db.execute("""UPDATE news_sources SET last_success=?,next_run=?,status='ready',
                error=NULL,failure_count=0,etag=?,last_modified=?,site_name=COALESCE(?,site_name) WHERE id=?""",
                (now, now + interval, etag, modified,
                 result.get("site_name"), source_id))

    def record_failure(self, source_id: str, *, now: float) -> None:
        with self.store.transaction(), self.store.db() as db:
            source = self.source(source_id)
            count = source["failure_count"] + 1
            delay = min(21600, 300 * 2 ** min(count - 1, 10))
            message = "뉴스 피드를 확인하지 못했습니다. 자동으로 다시 확인합니다."
            db.execute("UPDATE news_sources SET status='retry',error=?,failure_count=?,next_run=? WHERE id=?",
                       (message, count, now + delay, source_id))
            if source["error"] != message:
                self.store.notify(f"{self.store.profile(source['channel_id']).label}\n{message}\n수집원: {source_id}")

    def reject_item(self, item_id: str, reason: str) -> None:
        with self.store.db() as db:
            db.execute("UPDATE news_items SET status='unsupported',reason=? WHERE id=?", (reason[:500], item_id))

    def channel_state(self, channel_id: str) -> dict:
        with self.store.db() as db:
            count = db.execute("""SELECT count(*) FROM news_items i JOIN news_sources s ON s.id=i.source_id
                WHERE i.channel_id=? AND i.status='available' AND i.job_id IS NULL AND s.active=1""",
                (channel_id,)).fetchone()[0]
        times = [s["next_run"] for s in self.sources(channel_id) if s["active"]]
        return {"available": count, "next_run": min(times) if times else None, "auth_wait": False}
