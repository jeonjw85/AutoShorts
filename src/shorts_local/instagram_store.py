"""Channel-scoped source and material operations sharing the command transaction."""

import json
import re
import time
import uuid

from .config import Settings
from .instagram_models import (
    InstagramInput,
    ScanBatch,
    SourceMedia,
    normalize_username,
    parse_instagram_url,
)
from .store import Store


class InstagramStore:
    def __init__(self, store: Store):
        self.store = store

    def source(self, source_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{12}", source_id):
            raise ValueError("수집원 ID가 올바르지 않습니다.")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM instagram_sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise ValueError("수집원 ID를 찾을 수 없습니다.")
        result = dict(row)
        result["cursor"] = json.loads(result["cursor"]) if result["cursor"] else None
        return result

    def sources(self, channel_id: str = "default") -> list[dict]:
        self.store.profile(channel_id)
        with self.store.db() as db:
            ids = [r[0] for r in db.execute("""SELECT id FROM instagram_sources
                WHERE channel_id=? ORDER BY registered_at,id""", (channel_id,))]
        return [self.source(id) for id in ids]

    def add_source(self, username: str, *, channel_id: str = "default",
                   now: float | None = None) -> dict:
        username = normalize_username(username)
        now = time.time() if now is None else now
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            db.execute("""INSERT OR IGNORE INTO instagram_sources
                (id,channel_id,username,registered_at) VALUES (?,?,?,?)""",
                       (uuid.uuid4().hex[:12], channel_id, username, now))
            id = db.execute("SELECT id FROM instagram_sources WHERE channel_id=? AND username=?",
                            (channel_id, username)).fetchone()[0]
            return self.source(id)

    def set_source_active(self, source_id: str, active: bool) -> None:
        with self.store.transaction(), self.store.db() as db:
            source = self.source(source_id)
            if active and source["status"] == "duplicate":
                raise ValueError("이미 연결된 동일 계정의 기존 수집원을 사용하세요.")
            db.execute("""UPDATE instagram_sources SET active=?,
                status=CASE WHEN ? AND status='unavailable' THEN 'pending' ELSE status END,
                next_run=CASE WHEN ? THEN 0 ELSE next_run END WHERE id=?""",
                       (active, active, active, source_id))

    def materials(self, channel_id: str = "default", limit: int = 20) -> list[dict]:
        self.store.profile(channel_id)
        with self.store.db() as db:
            return [dict(row) for row in db.execute("""SELECT i.*,j.status AS job_status,
                j.stage AS job_stage FROM instagram_items i LEFT JOIN jobs j ON j.id=i.job_id
                WHERE i.channel_id=? ORDER BY i.discovered_at DESC,i.id LIMIT ?""",
                (channel_id, limit))]

    def _existing_key(self, channel_id, key):
        if key is None:
            return None
        with self.store.db() as db:
            row = db.execute("SELECT id FROM jobs WHERE channel_id=? AND dedup_key=?",
                             (channel_id, key)).fetchone()
            return row[0] if row else None

    def _reserve(self, item, mode, publish, key):
        if item["job_id"]:
            return item["job_id"]
        settings = self.store.profile(item["channel_id"]).settings
        settings = Settings.model_validate(settings.model_dump() | {
            "production_type": "instagram", "instagram_edit_mode": mode or settings.instagram_edit_mode,
        })
        input = InstagramInput(item_id=item["id"], source_id=item["source_id"],
                               shortcode=item["shortcode"], url=item["url"])
        return self.store.enqueue(settings, key, publish, channel_id=item["channel_id"], input=input)

    def import_video(self, url: str, *, channel_id: str = "default", mode: str | None = None,
                     publish: bool | None = None, key: str | None = None) -> str:
        shortcode, url = parse_instagram_url(url)
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            if existing := self._existing_key(channel_id, key):
                return existing
            db.execute("""INSERT OR IGNORE INTO instagram_items
                (id,channel_id,shortcode,url,discovered_at,status) VALUES (?,?,?,?,?,'available')""",
                (uuid.uuid4().hex[:12], channel_id, shortcode, url, time.time()))
            item = db.execute("SELECT * FROM instagram_items WHERE channel_id=? AND shortcode=?",
                              (channel_id, shortcode)).fetchone()
            return self._reserve(item, mode, publish, key)

    def enqueue_next(self, *, channel_id: str = "default", mode: str | None = None,
                     publish: bool | None = None, key: str | None = None) -> str | None:
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            if existing := self._existing_key(channel_id, key):
                return existing
            item = db.execute("""SELECT i.* FROM instagram_items i
                JOIN instagram_sources s ON s.id=i.source_id
                WHERE i.channel_id=? AND i.status='available' AND i.job_id IS NULL AND s.active=1
                ORDER BY i.published_at,i.shortcode LIMIT 1""", (channel_id,)).fetchone()
            return self._reserve(item, mode, publish, key) if item else None

    def apply_scan(self, batch: ScanBatch, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        batch = ScanBatch.model_validate(batch)
        with self.store.transaction(), self.store.db() as db:
            source = self.source(batch.source_id)
            if source["scan_revision"] != batch.base_revision:
                return False
            if source["instagram_id"] and batch.account_id not in (None, source["instagram_id"]):
                raise ValueError("수집원의 원래 계정과 조회 계정이 다릅니다.")
            duplicate = db.execute("""SELECT id FROM instagram_sources WHERE channel_id=?
                AND instagram_id=? AND id<>?""",
                (source["channel_id"], batch.account_id, source["id"])).fetchone()
            if duplicate:
                db.execute("""UPDATE instagram_sources SET active=0,status='duplicate',
                    error='이미 등록된 계정입니다.',scan_revision=scan_revision+1 WHERE id=?""",
                           (source["id"],))
                return True
            for post in batch.posts:
                if post.published_at < source["registered_at"]:
                    continue
                status = "available" if post.media_type == "video" else "unsupported"
                db.execute("""INSERT INTO instagram_items
                    (id,channel_id,source_id,shortcode,url,author,owner_id,published_at,
                     caption,media_type,discovered_at,status,reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,shortcode) DO UPDATE SET
                    source_id=COALESCE(instagram_items.source_id,excluded.source_id),
                    author=excluded.author,owner_id=excluded.owner_id,published_at=excluded.published_at,
                    caption=excluded.caption,media_type=excluded.media_type,
                    status=CASE WHEN instagram_items.job_id IS NOT NULL
                        OR instagram_items.status='unavailable' THEN instagram_items.status
                        ELSE excluded.status END""",
                    (uuid.uuid4().hex[:12], source["channel_id"], source["id"], post.shortcode,
                     post.url, post.author, post.owner_id, post.published_at, post.caption,
                     post.media_type, now, status, None if status == "available" else "단일 영상이 아닙니다."))
            interval = self.store.profile(source["channel_id"]).settings.instagram_poll_minutes * 60
            full_complete = (batch.complete and not batch.error_code
                             and (batch.cursor or {}).get("mode") != "recent")
            db.execute("""UPDATE instagram_sources SET instagram_id=COALESCE(instagram_id,?),
                resolved_username=COALESCE(?,resolved_username),cursor=?,scan_revision=scan_revision+1,
                completed_through=CASE WHEN ? THEN ? ELSE completed_through END,
                last_success=CASE WHEN ? THEN ? ELSE last_success END,
                next_run=?,status='ready',failure_count=CASE WHEN ? THEN 0 ELSE failure_count END,
                error=CASE WHEN ? THEN NULL ELSE error END WHERE id=?""",
                (batch.account_id, batch.username, json.dumps(batch.cursor) if batch.cursor else None,
                 full_complete, batch.started_at, not batch.error_code, now,
                 max(now + (interval if batch.complete else 300), batch.retry_at or 0),
                 not batch.error_code, not batch.error_code, source["id"]))
            if not batch.error_code:
                self._notice(source["channel_id"], None, source_id=source["id"])
            return True

    def record_media(self, item_id: str, media: SourceMedia, *, job_id: str) -> None:
        with self.store.transaction(), self.store.db() as db:
            job = self.store.get(job_id)
            item = db.execute("SELECT * FROM instagram_items WHERE id=?", (item_id,)).fetchone()
            if (not item or not job["input"] or job["input"]["item_id"] != item_id
                    or item["job_id"] != job_id or item["channel_id"] != job["channel_id"]
                    or media.post.shortcode != item["shortcode"]):
                raise ValueError("원본 메타데이터와 작업 입력이 다릅니다.")
            p = media.post
            db.execute("""UPDATE instagram_items SET author=?,owner_id=?,published_at=?,caption=?,
                media_type=?,status='reserved',reason=NULL WHERE id=?""",
                (p.author, p.owner_id, p.published_at, p.caption, p.media_type, item_id))

    def reject_item(self, item_id: str, code: str) -> None:
        reasons = {"unsupported": "지원하는 단일 영상이 아닙니다.",
                   "unavailable": "원본이 삭제되었거나 접근할 수 없습니다."}
        if code not in reasons:
            raise ValueError("지원하지 않는 소재 상태입니다.")
        with self.store.db() as db:
            db.execute("UPDATE instagram_items SET status=?,reason=? WHERE id=?",
                       (code, reasons[code], item_id))

    def request_scan(self, channel_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.store.transaction(), self.store.db() as db:
            self.store.profile(channel_id)
            db.execute("UPDATE instagram_sources SET next_run=? WHERE channel_id=? AND active=1",
                       (max(now, self.store.value("instagram_next_run", 0)), channel_id))

    def due_source(self, *, channel_id: str | None = None, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if channel_id is not None:
            self.store.profile(channel_id)
        if (self.store.value("paused", False) or self.store.value("instagram_auth_wait", False)
                or self.store.value("instagram_next_run", 0) > now):
            return None
        with self.store.db() as db:
            row = db.execute("""SELECT s.id FROM instagram_sources s JOIN channels c ON c.id=s.channel_id
                WHERE s.active=1 AND c.paused=0 AND s.status NOT IN ('unavailable','duplicate')
                AND s.next_run<=? AND (? IS NULL OR s.channel_id=?)
                ORDER BY s.next_run,s.registered_at,s.id LIMIT 1""",
                (now, channel_id, channel_id)).fetchone()
        return self.source(row[0]) if row else None

    def channel_state(self, channel_id: str) -> dict:
        sources = self.sources(channel_id)
        with self.store.db() as db:
            available = db.execute("""SELECT count(*) FROM instagram_items i
                JOIN instagram_sources s ON s.id=i.source_id WHERE i.channel_id=?
                AND i.status='available' AND i.job_id IS NULL AND s.active=1""", (channel_id,)).fetchone()[0]
            reserved = db.execute("SELECT count(*) FROM instagram_items WHERE channel_id=? AND job_id IS NOT NULL",
                                  (channel_id,)).fetchone()[0]
        upcoming = [s["next_run"] for s in sources if s["active"]]
        return {"available": available, "reserved": reserved, "sources": len(sources),
                "auth_wait": bool(self.store.value("instagram_auth_wait", False)),
                "next_run": max(min(upcoming), self.store.value("instagram_next_run", 0)) if upcoming else None}

    def _notice(self, channel_id, code, *, source_id=None, job_id=None):
        from .instagram import MESSAGES

        alerts = self.store.value("instagram_alerts", {})
        key = f"{channel_id}:{source_id or 'jobs'}"
        if code in {"busy", "deferred"}:
            return
        if code is None:
            alerts.pop(key, None)
        else:
            if code == "auth_wait":
                key = f"{channel_id}:auth"
            if alerts.get(key) == code:
                return
            alerts[key] = code
            text = MESSAGES.get(code, MESSAGES["transient"])
            if source_id:
                text += f"\n수집원: {source_id}"
            if job_id:
                text += f"\n작업: {job_id}"
            else:
                text = f"{self.store.profile(channel_id).label}\n{text}"
            self.store.notify(text, job_id)
        self.store.set_value("instagram_alerts", alerts)

    def _wait_state(self, code, retry_at, now, auth_generation):
        from .instagram import session_generation

        if (code == "auth_wait" and auth_generation is not None
                and auth_generation != session_generation(self.store.root)):
            code, retry_at = "deferred", now + 1
        if code == "auth_wait":
            self.store.set_value("instagram_auth_wait", True)
        if code in {"rate_limit", "deferred"}:
            retry_at = retry_at if retry_at is not None else now + (3600 if code == "rate_limit" else 5)
            retry_at = max(retry_at, self.store.value("instagram_next_run", 0))
            self.store.set_value("instagram_next_run", retry_at)
        return code, retry_at

    def record_failure(self, source_id: str, code: str, *, retry_at: float | None = None,
                       now: float | None = None, auth_generation: str | None = None) -> None:
        from .instagram import MESSAGES

        now = time.time() if now is None else now
        with self.store.transaction(), self.store.db() as db:
            source = self.source(source_id)
            code, retry_at = self._wait_state(code, retry_at, now, auth_generation)
            count = source["failure_count"] + (code == "transient")
            delay = min(21600, 300 * 2 ** min(max(0, count - 1), 10)) if code == "transient" else 5
            status = code if code in {"auth_wait", "unavailable"} else "retry"
            db.execute("""UPDATE instagram_sources SET status=?,error=?,failure_count=?,next_run=?
                WHERE id=?""", (status, MESSAGES.get(code, MESSAGES["transient"]), count,
                               max(now + delay, retry_at or 0), source_id))
            self._notice(source["channel_id"], code, source_id=source_id)

    def wait_job(self, job_id: str, code: str, *, retry_at: float | None = None,
                 auth_generation: str | None = None) -> None:
        from .instagram import MESSAGES

        if code not in {"busy", "deferred", "rate_limit", "auth_wait"}:
            raise ValueError("재시도 횟수를 보존하는 대기 사유가 아닙니다.")
        now = time.time()
        with self.store.transaction():
            job = self.store.get(job_id)
            if job["status"] != "running":
                return
            code, retry_at = self._wait_state(code, retry_at, now, auth_generation)
            self.store.update(job_id, status="auth_wait" if code == "auth_wait" else "retry",
                              attempts=max(0, job["attempts"] - 1), error=MESSAGES[code],
                              next_run=max(now + 1, retry_at or now + 5))
            self._notice(job["channel_id"], code, job_id=job_id)

    def reconcile_auth(self) -> None:
        from .instagram import session_generation

        generation = session_generation(self.store.root)
        if not generation:
            return
        with self.store.transaction(), self.store.db() as db:
            if generation == self.store.value("instagram_auth_generation"):
                return
            db.execute("""UPDATE jobs SET status='queued',next_run=0,error=NULL
                WHERE status='auth_wait'""")
            db.execute("""UPDATE instagram_sources SET status='pending',next_run=0,error=NULL
                WHERE status='auth_wait'""")
            self.store.set_value("instagram_auth_wait", False)
            alerts = self.store.value("instagram_alerts", {})
            self.store.set_value("instagram_alerts", {k: v for k, v in alerts.items() if v != "auth_wait"})
            self.store.set_value("instagram_auth_generation", generation)
