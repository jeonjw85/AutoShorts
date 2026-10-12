import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .config import Settings
from .process import BlockedError, exclusive_lock

JOBS_SQL = """CREATE TABLE jobs (
    id TEXT PRIMARY KEY, dedup_key TEXT, settings TEXT NOT NULL,
    publish INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'plan', attempts INTEGER NOT NULL DEFAULT 0,
    next_run REAL NOT NULL DEFAULT 0, error TEXT, title TEXT,
    fingerprint TEXT, video_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
    manual_run INTEGER NOT NULL DEFAULT 0,
    channel_id TEXT NOT NULL DEFAULT 'default' REFERENCES channels(id),
    target_youtube_id TEXT, legacy_upload INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id, dedup_key), UNIQUE(channel_id, fingerprint)
)"""


def _copy_legacy_auth(root: Path) -> None:
    source = root / "auth/youtube.json"
    if not source.exists():
        return
    destination = root / "channels/default/auth/youtube.json"
    content = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != content:
            raise BlockedError("기존 채널 인증 파일과 이전 대상이 다릅니다. 파일을 확인하세요.")
        destination.chmod(0o600)
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".migrate-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def _backup(db: sqlite3.Connection, root: Path, name: str = "pre-channels.sqlite3"):
    destination = root / "backups" / name
    if destination.exists():
        with closing(sqlite3.connect(destination)) as backup:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BlockedError("기존 이전 백업을 읽을 수 없습니다.")
        return
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".backup-")
    os.close(fd)
    try:
        with closing(sqlite3.connect(name)) as backup:
            db.backup(backup)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def _migrate_instagram(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE jobs ADD COLUMN input TEXT")
    db.execute("""CREATE TABLE instagram_sources (
        id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
        username TEXT NOT NULL, instagram_id TEXT, resolved_username TEXT,
        active INTEGER NOT NULL DEFAULT 1, registered_at REAL NOT NULL,
        next_run REAL NOT NULL DEFAULT 0, last_success REAL, error TEXT, cursor TEXT,
        completed_through REAL, status TEXT NOT NULL DEFAULT 'pending',
        failure_count INTEGER NOT NULL DEFAULT 0, scan_revision INTEGER NOT NULL DEFAULT 0,
        UNIQUE(channel_id,username), UNIQUE(channel_id,instagram_id)
    )""")
    db.execute("""CREATE TABLE instagram_items (
        id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
        source_id TEXT REFERENCES instagram_sources(id), shortcode TEXT NOT NULL,
        url TEXT NOT NULL, author TEXT, owner_id TEXT, published_at REAL, caption TEXT,
        media_type TEXT, discovered_at REAL NOT NULL, status TEXT NOT NULL,
        reason TEXT, job_id TEXT UNIQUE REFERENCES jobs(id), UNIQUE(channel_id,shortcode)
    )""")


def _upgrade_instagram(db: sqlite3.Connection, root: Path) -> None:
    _backup(db, root, "pre-instagram.sqlite3")
    with db:
        db.execute("BEGIN IMMEDIATE")
        _migrate_instagram(db)
        db.execute("PRAGMA user_version=2")


def _upgrade_news(db: sqlite3.Connection, root: Path) -> None:
    _backup(db, root, "pre-news.sqlite3")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE news_sources (
            id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
            feed_url TEXT NOT NULL, site_name TEXT, active INTEGER NOT NULL DEFAULT 1,
            registered_at REAL NOT NULL, next_run REAL NOT NULL DEFAULT 0,
            last_success REAL, error TEXT, etag TEXT, last_modified TEXT,
            status TEXT NOT NULL DEFAULT 'pending', failure_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel_id,feed_url)
        )""")
        db.execute("""CREATE TABLE news_items (
            id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
            source_id TEXT REFERENCES news_sources(id), canonical_url TEXT NOT NULL,
            title TEXT, published_at REAL, discovered_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('available','reserved','unsupported','unavailable')),
            reason TEXT, job_id TEXT UNIQUE REFERENCES jobs(id), UNIQUE(channel_id,canonical_url)
        )""")
        db.execute("PRAGMA user_version=3")


def ensure_database(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    path = root / "state.sqlite3"
    with closing(sqlite3.connect(path, timeout=15)) as db:
        path.chmod(0o600)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 3:
            return
        if version > 3:
            raise BlockedError("더 새로운 DB 형식입니다. 프로그램을 업데이트하세요.")
        with (exclusive_lock(root / "migration.lock"), exclusive_lock(root / "service.lock"),
              exclusive_lock(root / "worker.lock")):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                return
            if version > 3:
                raise BlockedError("더 새로운 DB 형식입니다. 프로그램을 업데이트하세요.")
            if version == 1:
                _upgrade_instagram(db, root)
                version = 2
            if version == 2:
                _upgrade_news(db, root)
                return
            old = db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
            settings = Settings.load(root / "config.json")
            if old:
                _backup(db, root)
            _copy_legacy_auth(root)
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE channels (
                    id TEXT PRIMARY KEY, settings TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                    youtube_channel_id TEXT UNIQUE, youtube_title TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                )""")
                now = time.time()
                db.execute("INSERT INTO channels (id,settings,created,updated) VALUES ('default',?,?,?)",
                           (settings.model_dump_json(), now, now))
                if old:
                    db.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
                db.execute(JOBS_SQL)
                if old:
                    columns = [row[1] for row in db.execute("PRAGMA table_info(jobs_legacy)")]
                    names = ",".join(columns)
                    db.execute(f"INSERT INTO jobs ({names}) SELECT {names} FROM jobs_legacy")
                    for (job_id,) in db.execute("SELECT id FROM jobs").fetchall():
                        folder = root / "jobs" / job_id
                        if any((folder / name).exists() for name in (
                                "upload-state.json", "upload-receipt.json")):
                            db.execute("UPDATE jobs SET legacy_upload=1 WHERE id=?", (job_id,))
                    db.execute("DROP TABLE jobs_legacy")
                db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, job_id TEXT, video TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_run REAL NOT NULL DEFAULT 0
                )""")
                if "video" not in {r[1] for r in db.execute("PRAGMA table_info(notifications)")}:
                    db.execute("ALTER TABLE notifications ADD COLUMN video TEXT")
                db.execute("""CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY, reply TEXT NOT NULL
                )""")
                db.execute("PRAGMA user_version=1")
            _upgrade_instagram(db, root)
            _upgrade_news(db, root)
