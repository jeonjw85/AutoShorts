import pytest


@pytest.fixture
def legacy_home(tmp_path):
    import json
    import sqlite3

    from shorts_local.config import Settings

    Settings(topic="보존할 기존 주제").save(tmp_path / "config.json")
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.executescript("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, settings TEXT NOT NULL,
                publish INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'plan', attempts INTEGER NOT NULL DEFAULT 0,
                next_run REAL NOT NULL DEFAULT 0, error TEXT, title TEXT,
                fingerprint TEXT UNIQUE, video_id TEXT, created REAL NOT NULL,
                updated REAL NOT NULL, manual_run INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY, text TEXT NOT NULL, job_id TEXT, video TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_run REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE telegram_updates (update_id INTEGER PRIMARY KEY, reply TEXT NOT NULL);
        """)
        for job, status, stage in (("legacy-ready", "ready", "complete"),
                                   ("legacy-upload", "retry", "upload")):
            db.execute("""INSERT INTO jobs
                (id,settings,publish,status,stage,created,updated,attempts,dedup_key)
                VALUES (?,?,1,?,?,1,2,1,?)""",
                (job, Settings(topic="작업 당시 주제").model_dump_json(), status, stage,
                 "schedule:2020-01-01" if stage == "upload" else None))
        db.executemany("INSERT INTO state VALUES (?,?)", [("paused", "true"),
                                                          ("telegram_offset", "101")])
        db.execute("INSERT INTO telegram_updates VALUES (100,?)", (json.dumps({"text": "접수"}),))
        db.execute("INSERT INTO notifications (text,job_id) VALUES ('완성','legacy-ready')")
    folder = tmp_path / "jobs/legacy-ready"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"unchanged-video")
    folder = tmp_path / "jobs/legacy-upload"
    folder.mkdir()
    (folder / "upload-state.json").write_text('{"url":"old-session"}')
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth/youtube.json").write_text('{"refresh_token":"fixture-only"}')
    return tmp_path


@pytest.fixture
def story_data():
    return {
        "title": "내일 도착한 편지",
        "description": "오래된 우체통에서 시작된 짧은 창작 이야기입니다.",
        "scenes": [
            {"heading": "이상한 우체통", "narration": "비가 그친 골목에서 내 이름이 적힌 편지를 발견했어요.",
             "image_prompt": "A red mailbox in a quiet rain-soaked Korean alley, cinematic illustration"},
            {"heading": "내일의 날짜", "narration": "봉투에 적힌 날짜는 내일이었고, 안에는 꽃씨 하나가 들어 있었죠.",
             "image_prompt": "Close-up of an envelope and a tiny seed on a wooden table, warm light"},
            {"heading": "작은 시작", "narration": "나는 빈 화분에 씨앗을 심었어요. 내일을 기다리는 이유가 생겼어요.",
             "image_prompt": "A small green sprout by a sunny window, warm cinematic illustration"},
        ],
    }


@pytest.fixture
def schema1_home(legacy_home):
    """Frozen schema-1 fixture; never initialize it through the current Store."""
    import json
    import sqlite3

    root = legacy_home
    (root / "backups").mkdir()
    with sqlite3.connect(root / "state.sqlite3") as db:
        with sqlite3.connect(root / "backups/pre-channels.sqlite3") as backup:
            db.backup(backup)
        db.executescript("""
            CREATE TABLE channels (
                id TEXT PRIMARY KEY, settings TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                youtube_channel_id TEXT UNIQUE, youtube_title TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            ALTER TABLE jobs RENAME TO old_jobs;
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, dedup_key TEXT, settings TEXT NOT NULL,
                publish INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'plan', attempts INTEGER NOT NULL DEFAULT 0,
                next_run REAL NOT NULL DEFAULT 0, error TEXT, title TEXT,
                fingerprint TEXT, video_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                manual_run INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT NOT NULL DEFAULT 'default' REFERENCES channels(id),
                target_youtube_id TEXT, legacy_upload INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel_id,dedup_key), UNIQUE(channel_id,fingerprint)
            );
        """)
        config = json.loads((root / "config.json").read_text())
        old_fields = {"production_type", "instagram_edit_mode", "instagram_style",
                      "instagram_accent_color", "instagram_poll_minutes", "vision_model",
                      "transcription_model"}
        config = {k: v for k, v in config.items() if k not in old_fields}
        for channel in ("default", "animals"):
            db.execute("INSERT INTO channels (id,settings,created,updated) VALUES (?,?,1,1)",
                       (channel, json.dumps(config, ensure_ascii=False)))
        columns = ",".join(r[1] for r in db.execute("PRAGMA table_info(old_jobs)"))
        db.execute(f"INSERT INTO jobs ({columns}) SELECT {columns} FROM old_jobs")
        db.execute("DROP TABLE old_jobs")
        db.execute("UPDATE jobs SET legacy_upload=1 WHERE id='legacy-upload'")
        db.execute("PRAGMA user_version=1")
    (root / "backups/pre-channels.sqlite3").chmod(0o600)
    return root
