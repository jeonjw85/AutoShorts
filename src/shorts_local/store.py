from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .health import MAX_NOTIFICATION_ATTEMPTS, append_alert
from .instagram_models import InstagramInput
from .migrations import ensure_database
from .models import Story
from .news_models import NewsInput
from .process import BlockedError, atomic_json
from .profiles import ChannelProfile, validate_channel_id, validate_youtube_id

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self._active_db: ContextVar[sqlite3.Connection | None] = ContextVar("database", default=None)
        ensure_database(root)
        self.path = root / "state.sqlite3"

    @contextmanager
    def db(self):
        active = self._active_db.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        if self._active_db.get() is not None:
            yield
            return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            token = self._active_db.set(db)
            try:
                yield
            finally:
                self._active_db.reset(token)

    def profile(self, channel_id: str = "default") -> ChannelProfile:
        validate_channel_id(channel_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        if row is None:
            raise ValueError("채널 ID를 찾을 수 없습니다.")
        return ChannelProfile(id=row["id"], settings=Settings.model_validate_json(row["settings"]),
                              paused=bool(row["paused"]), youtube_channel_id=row["youtube_channel_id"],
                              youtube_title=row["youtube_title"])

    def profiles(self) -> list[ChannelProfile]:
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM channels ORDER BY created,id")]
        return [self.profile(channel_id) for channel_id in ids]

    def add_profile(self, channel_id: str, name: str) -> ChannelProfile:
        validate_channel_id(channel_id)
        settings = Settings(channel_name=name)
        now = time.time()
        try:
            with self.transaction(), self.db() as db:
                db.execute("INSERT INTO channels (id,settings,created,updated) VALUES (?,?,?,?)",
                           (channel_id, settings.model_dump_json(), now, now))
                return self.profile(channel_id)
        except sqlite3.IntegrityError:
            raise ValueError("이미 등록된 채널 ID입니다.") from None

    def update_profile_settings(self, channel_id: str, changes: dict) -> ChannelProfile:
        with self.transaction(), self.db() as db:
            profile = self.profile(channel_id)
            settings = Settings.model_validate({**profile.settings.model_dump(), **changes})
            db.execute("UPDATE channels SET settings=?,updated=? WHERE id=?",
                       (settings.model_dump_json(), time.time(), channel_id))
            return self.profile(channel_id)

    def set_paused(self, paused: bool, *, channel_id: str | None = None) -> None:
        with self.transaction(), self.db() as db:
            if channel_id is None:
                self.set_value("paused", paused)
            else:
                self.profile(channel_id)
                db.execute("UPDATE channels SET paused=?,updated=? WHERE id=?",
                           (paused, time.time(), channel_id))

    def bind_channel(self, channel_id: str, youtube_id: str, title: str) -> ChannelProfile:
        validate_youtube_id(youtube_id)
        try:
            with self.transaction(), self.db() as db:
                profile = self.profile(channel_id)
                if profile.youtube_channel_id not in (None, youtube_id):
                    raise ValueError("다른 YouTube 채널입니다. 별도 프로필로 연결하세요.")
                db.execute("UPDATE channels SET youtube_channel_id=?,youtube_title=?,updated=? WHERE id=?",
                           (youtube_id, title, time.time(), channel_id))
                return self.profile(channel_id)
        except sqlite3.IntegrityError:
            raise ValueError("이미 다른 프로필에 연결된 YouTube 채널입니다.") from None

    def bind_job_target(self, job_id: str, youtube_id: str) -> str:
        with self.transaction(), self.db() as db:
            job = self.get(job_id)
            profile = self.profile(job["channel_id"])
            if (not youtube_id or profile.youtube_channel_id != youtube_id
                    or job["target_youtube_id"] not in (None, youtube_id)):
                raise BlockedError("작업의 업로드 대상과 연결된 YouTube 채널이 다릅니다.")
            db.execute("UPDATE jobs SET target_youtube_id=?,updated=? WHERE id=?",
                       (youtube_id, time.time(), job_id))
            return youtube_id

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        result["input"] = json.loads(result["input"]) if result.get("input") else None
        return result

    def enqueue(self, settings: Settings | None = None, key: str | None = None,
                publish: bool | None = None, story: Story | None = None, *,
                channel_id: str = "default", topic: str | None = None,
                input: InstagramInput | NewsInput | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.transaction(), self.db() as db:
            profile = self.profile(channel_id)
            if key is not None:
                existing = db.execute("SELECT id FROM jobs WHERE channel_id=? AND dedup_key=?",
                                      (channel_id, key)).fetchone()
                if existing:
                    return existing[0]
            settings = settings or profile.settings
            if settings.production_type == "instagram":
                if input is None or story is not None or topic is not None:
                    raise ValueError("인스타그램 작업에는 소재 입력만 사용할 수 있습니다.")
                input = InstagramInput.model_validate(input)
                item = db.execute("SELECT * FROM instagram_items WHERE id=?",
                                  (input.item_id,)).fetchone()
                if (item is None or item["channel_id"] != channel_id
                        or item["shortcode"] != input.shortcode or item["url"] != input.url
                        or item["source_id"] != input.source_id):
                    raise ValueError("작업 입력과 소재의 채널/원본이 다릅니다.")
                if item["job_id"]:
                    return item["job_id"]
                if item["status"] != "available":
                    raise ValueError("이 소재는 제작할 수 없습니다.")
            elif settings.production_type == "news":
                if input is None or story is not None or topic is not None:
                    raise ValueError("뉴스 작업에는 기사 입력만 사용할 수 있습니다.")
                input = NewsInput.model_validate(input)
                item = db.execute("SELECT * FROM news_items WHERE id=?", (input.item_id,)).fetchone()
                if (item is None or item["channel_id"] != channel_id
                        or item["canonical_url"] != input.url or item["source_id"] != input.source_id
                        or item["published_at"] != input.published_at):
                    raise ValueError("작업 입력과 기사의 채널/원본이 다릅니다.")
                if item["job_id"]:
                    return item["job_id"]
                if item["status"] != "available":
                    raise ValueError("이 기사는 제작할 수 없습니다.")
            elif input is not None:
                raise ValueError("이야기 작업에는 외부 소재를 사용할 수 없습니다.")
            if topic is not None:
                settings = Settings.model_validate({**settings.model_dump(), "topic": topic})
            publish = settings.youtube_enabled if publish is None else publish
            inserted = db.execute("""INSERT OR IGNORE INTO jobs
                (id, dedup_key, settings, publish, created, updated, channel_id, target_youtube_id,
                 input, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, key, settings.model_dump_json(), publish, now, now,
                  channel_id, profile.youtube_channel_id, input.model_dump_json() if input else None,
                  "fetch" if input else "plan"))
            if input is not None and inserted.rowcount:
                atomic_json(self.root / "jobs" / job_id / "input.json", input.model_dump())
                table = "news_items" if settings.production_type == "news" else "instagram_items"
                db.execute(f"UPDATE {table} SET status='reserved',job_id=? WHERE id=?",
                           (job_id, input.item_id))
            if story is not None and inserted.rowcount:
                # The transaction is still invisible to claimers until the artifact exists.
                atomic_json(self.root / "jobs" / job_id / "story.json", story.model_dump())
            if key is not None:
                job_id = db.execute("SELECT id FROM jobs WHERE channel_id=? AND dedup_key=?",
                                    (channel_id, key)).fetchone()[0]
        return job_id

    def get(self, job_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("작업 ID를 찾을 수 없습니다.")
        return self.decode(row)

    def list(self, limit: int = 20, *, channel_id: str | None = None) -> list[dict]:
        if channel_id is not None:
            self.profile(channel_id)
        with self.db() as db:
            return [self.decode(row) for row in db.execute(
                """SELECT * FROM jobs WHERE (? IS NULL OR channel_id=?)
                   ORDER BY created DESC,id LIMIT ?""", (channel_id, channel_id, limit))]

    def claim(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.transaction(), self.db() as db:
            if self.value("paused", False):
                return None
            candidates = db.execute("""SELECT jobs.* FROM jobs JOIN channels ON channels.id=jobs.channel_id
                WHERE status IN ('queued', 'retry') AND channels.paused=0
                AND next_run<=? AND attempts<3 ORDER BY jobs.created,jobs.id""", (now,)).fetchall()
            for row in candidates:
                key = row["dedup_key"] or ""
                folder = self.root / "jobs" / row["id"]
                has_upload = any((folder / name).exists() for name in (
                    "upload-state.json", "upload-receipt.json"))
                if (key.startswith("schedule:") and not row["manual_run"]
                        and row["stage"] != "upload" and not has_upload):
                    zone = ZoneInfo(json.loads(row["settings"])["timezone"])
                    today = datetime.fromtimestamp(now, zone).date().isoformat()
                    if key.removeprefix("schedule:") < today:
                        db.execute("UPDATE jobs SET status='skipped', error=?, updated=? WHERE id=?",
                                   ("이전 날짜의 미완성 예약 작업을 건너뛰었습니다.", now, row["id"]))
                        continue
                break
            else:
                return None
            db.execute("""UPDATE jobs SET status='running', attempts=attempts+1,
                updated=?, error=NULL WHERE id=?""", (now, row["id"]))
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def update(self, job_id: str, **values) -> None:
        allowed = {"status", "stage", "error", "next_run", "title", "fingerprint", "video_id",
                   "attempts", "publish", "manual_run"}
        if not values or not values.keys() <= allowed:
            raise ValueError("허용되지 않은 작업 상태 변경입니다.")
        values["updated"] = time.time()
        with self.db() as db:
            columns = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), job_id))

    def recover(self) -> None:
        # Caller must hold the worker lock; a live worker must never be reclaimed.
        with self.db() as db:
            db.execute("""UPDATE jobs SET status=CASE WHEN attempts>=3 THEN 'failed'
                ELSE 'queued' END WHERE status='running'""")

    def fail(self, job_id: str, error: str, delay: int = 60, now: float | None = None):
        job = self.get(job_id)
        now = time.time() if now is None else now
        self.update(job_id, status="failed" if job["attempts"] >= 3 else "retry",
                    error=error[:500], next_run=now + delay)

    def retry(self, job_id: str):
        job = self.get(job_id)
        if job["status"] not in {"failed", "blocked", "ready", "retry", "skipped", "auth_wait"}:
            raise ValueError("이 작업은 재시도할 수 없습니다.")
        self.update(job_id, status="queued", attempts=0, next_run=0, error=None, manual_run=True)

    def value(self, key: str, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_value(self, key: str, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))

    def notify(self, text: str, job_id: str | None = None, video: str | None = None):
        if job_id is not None:
            job = self.get(job_id)
            text = (f"{self.profile(job['channel_id']).label} · "
                    f"{Settings.model_validate(job['settings']).production_label}\n{text}")
        with self.db() as db:
            db.execute("INSERT INTO notifications (text, job_id, video) VALUES (?, ?, ?)",
                       (text[:3800], job_id, video))

    def telegram_reply(self, update_id: int) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT reply FROM telegram_updates WHERE update_id=?", (update_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def accept_telegram(self, update_id: int, reply: dict):
        # Called inside the command transaction: mutations, reply, and acknowledgement commit together.
        with self.db() as db:
            db.execute("INSERT INTO telegram_updates VALUES (?, ?)", (update_id, json.dumps(reply)))
        self.notify(reply["text"], video=reply.get("video"))
        self.set_value("telegram_offset", max(self.value("telegram_offset", 0), update_id + 1))

    def pending_notifications(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM notifications WHERE next_run<=? ORDER BY id LIMIT 5",
                (time.time(),))]

    def notifications(self, limit: int = 100) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM notifications ORDER BY id LIMIT ?", (limit,))]

    def notification_count(self) -> int:
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    def discard_notification(self, note_id: int) -> None:
        with self.db() as db:
            db.execute("DELETE FROM notifications WHERE id=?", (note_id,))

    def queue_depth(self) -> int:
        with self.db() as db:
            return db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','retry')").fetchone()[0]

    def notification_result(self, note_id: int, success: bool) -> bool:
        """Return True while the notification stays queued for another attempt."""
        with self.db() as db:
            if success:
                db.execute("DELETE FROM notifications WHERE id=?", (note_id,))
                return False
            row = db.execute("SELECT attempts, text, job_id FROM notifications WHERE id=?",
                             (note_id,)).fetchone()
            if row is None:
                return False
            attempts = row["attempts"] + 1
            if attempts >= MAX_NOTIFICATION_ATTEMPTS:
                try:
                    append_alert(self.root, row["text"], job_id=row["job_id"])
                except OSError:
                    logger.error("알림 파일을 쓸 수 없어 알림을 폐기합니다: %s", row["text"][:200])
                db.execute("DELETE FROM notifications WHERE id=?", (note_id,))
                return False
            db.execute("UPDATE notifications SET attempts=?, next_run=? WHERE id=?",
                       (attempts, time.time() + 300, note_id))
            return True
