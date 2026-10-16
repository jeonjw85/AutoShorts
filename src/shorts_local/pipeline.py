import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from .artifacts import artifact_key
from .config import Settings
from .engines import images_complete, speech_complete
from .instagram import InstagramError
from .instagram_store import InstagramStore
from .models import Story, VideoMetadata
from .news_fetch import UnsupportedArticle
from .planning import plan_story
from .process import BlockedError, QuotaError, atomic_json, exclusive_lock, run_process
from .profiles import UploadTarget
from .render import render_video, validate_video
from .store import Store
from .youtube import (
    YouTubeUploader,
    auth_path,
    completed_upload,
    file_hash,
    identify_channel,
    legacy_token_provider,
    read_auth,
    read_upload_artifact,
    token_provider,
)


def engine(stage: str, workdir: Path, settings: Settings):
    required = {"speech": "mlx_audio", "images": "mflux"}[stage]
    if importlib.util.find_spec(required) is None:
        extra = "mlx" if stage == "speech" else "images"
        raise BlockedError(f"로컬 엔진 설치가 필요합니다: uv sync --extra mlx --extra {extra}")
    run_process([sys.executable, "-m", "shorts_local.engines", stage, str(workdir)],
                 workdir / f"{stage}.log", settings.stage_timeout)


def publish_video(store: Store, job: dict, video: Path, metadata: VideoMetadata, folder: Path) -> str:
    channel_id = job["channel_id"]
    target = (UploadTarget(channel_id=channel_id, youtube_id=job["target_youtube_id"])
              if job["target_youtube_id"] else None)
    legacy = bool(job["legacy_upload"]) and channel_id == "default"
    receipt = completed_upload(video, folder, target=target, legacy=legacy)
    if receipt:
        return receipt
    profile = store.profile(channel_id)
    auth = read_auth(auth_path(store.root, channel_id))
    state = folder / "upload-state.json"
    legacy_state = (legacy and state.exists() and not
                    ({"channel_id", "youtube_channel_id"} & read_upload_artifact(state).keys()))
    if "youtube_channel_id" not in auth or legacy_state:
        if channel_id != "default":
            raise BlockedError("채널 인증에 대상 정보가 없습니다. 다시 로그인하세요.")
        original = identify_channel(legacy_token_provider(store.root))
        if (profile.youtube_channel_id not in (None, original.id)
                or job["target_youtube_id"] not in (None, original.id)):
            raise BlockedError("기존 업로드 인증과 현재 연결 대상이 다릅니다. Studio에서 확인하세요.")
        try:
            profile = store.bind_channel(channel_id, original.id, original.title)
        except ValueError:
            raise BlockedError("기존 인증의 채널 바인딩을 복구할 수 없습니다.") from None
    if not profile.youtube_channel_id:
        raise BlockedError("YouTube 채널 연결이 완료되지 않았습니다. 해당 프로필로 다시 로그인하세요.")
    if auth.get("youtube_channel_id") not in (None, profile.youtube_channel_id):
        raise BlockedError("인증 파일과 프로필의 YouTube 채널이 다릅니다.")
    token = token_provider(store.root, channel_id)
    identity = identify_channel(token)
    youtube_id = store.bind_job_target(job["id"], identity.id)
    target = UploadTarget(channel_id=channel_id, youtube_id=youtube_id)
    with YouTubeUploader(token) as uploader:
        return uploader.upload(video, metadata, Settings.model_validate(job["settings"]), folder,
                               target=target, legacy=legacy_state)


def _story_key(job: dict) -> str:
    # Use the stored snapshot, not defaults introduced by a later application version.
    return artifact_key("story-job", {key: job[key] for key in ("id", "channel_id", "settings")})


def _seal_story_video(job: dict, folder: Path, report: dict):
    timeline = folder / "timeline.json"
    atomic_json(folder / "validation.json", {
        "schema": 1, "kind": "story-final", "job_key": _story_key(job),
        "video_sha256": file_hash(folder / "video.mp4"),
        "story_sha256": file_hash(folder / "story.json"),
        "timeline_sha256": file_hash(timeline) if timeline.exists() else None, "media": report,
    })


def _ready_story_video(job: dict, folder: Path, settings: Settings):
    video, script, timeline = folder / "video.mp4", folder / "story.json", folder / "timeline.json"
    uploads = [folder / name for name in ("upload-state.json", "upload-receipt.json")
               if (folder / name).exists()]
    if not video.exists() and not uploads and not (folder / "validation.json").exists():
        return None
    try:
        path = folder / "validation.json"
        seal = json.loads(path.read_text()) if path.exists() else None
        if isinstance(seal, dict) and ("schema" in seal or "kind" in seal):
            if (seal["schema"] != 1 or seal["kind"] != "story-final"
                    or seal["job_key"] != _story_key(job)):
                raise ValueError("이야기 완성 기록의 작업/설정이 다릅니다.")
            try:
                script_hash = file_hash(script)
            except OSError:
                raise BlockedError("완성한 이야기의 대본이 없습니다. 원본 대본을 복구하세요.") from None
            if seal["story_sha256"] != script_hash:
                raise BlockedError("완성한 이야기의 대본이 변경되었습니다. 새 대본은 새 작업으로 제작하세요.")
            if (seal["video_sha256"] != file_hash(video)
                    or timeline.exists() and seal["timeline_sha256"] != file_hash(timeline)):
                raise ValueError("이야기 완성 기록과 파일이 다릅니다.")
            validate_video(video, settings)
        else:
            # Older story outputs have only a media report (or no report after a crash).
            # Verify actual bytes before adopting them; never regenerate an upload's source.
            if seal is not None and (not isinstance(seal, dict) or not
                    {"duration", "width", "height", "audio_codec", "video_codec"} <= seal.keys()):
                raise ValueError("이야기 완성 기록을 읽을 수 없습니다.")
            digest = file_hash(video)
            for upload in uploads:
                record = read_upload_artifact(upload)
                if (record.get("sha256") != digest or upload.name == "upload-state.json"
                        and record.get("size") != video.stat().st_size):
                    raise BlockedError("업로드 중인 이야기의 영상 파일과 복구 기록이 다릅니다.")
            report = validate_video(video, settings, full_decode=True)
            Story.model_validate_json(script.read_text())
            _seal_story_video(job, folder, report)
        return video, Story.model_validate_json(script.read_text())
    except BlockedError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        if uploads:
            raise BlockedError("업로드가 시작된 이야기의 완성 기록/파일이 다릅니다. 복구 확인이 필요합니다.") from None
        return None


def _prepare_story(store: Store, job: dict, folder: Path, settings: Settings):
    ready = _ready_story_video(job, folder, settings)
    if ready:
        video, story = ready
    else:
        if shutil.disk_usage(folder).free < 5 * 1024**3:
            raise BlockedError("디스크 여유 공간이 5GB 미만입니다. 공간 확보 후 재시도하세요.")
        store.update(job["id"], stage="plan")
        if (folder / "story.json").exists():
            story = Story.model_validate_json((folder / "story.json").read_text())
        else:
            recent = [item["title"] for item in store.list(100, channel_id=job["channel_id"])
                      if item["title"] and item["id"] != job["id"]]
            story = plan_story(settings, recent, folder)
    fingerprint = hashlib.sha256("\n".join(x.narration for x in story.scenes).encode()).hexdigest()
    try:
        store.update(job["id"], title=story.title, fingerprint=fingerprint)
    except sqlite3.IntegrityError:
        raise BlockedError("다른 작업과 동일한 대본입니다. 새 작업을 생성하세요.") from None
    if ready:
        return video, story
    store.update(job["id"], stage="speech")
    if not speech_complete(story, folder):
        engine("speech", folder, settings)
    store.update(job["id"], stage="images")
    if settings.visuals == "mflux" and not images_complete(story, folder):
        engine("images", folder, settings)
    store.update(job["id"], stage="render")
    video = render_video(story, folder, settings)
    _seal_story_video(job, folder, validate_video(video, settings))
    return video, story


def execute_job(store: Store, job: dict):
    settings = Settings.model_validate(job["settings"])
    folder = (store.root / "jobs" / job["id"]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    settings.save(folder / "settings.json")
    if settings.production_type == "instagram":
        from .import_pipeline import prepare_imported_video

        video, metadata = prepare_imported_video(store, job, settings, folder)
        store.update(job["id"], title=metadata.title)
    elif settings.production_type == "news":
        from .news_pipeline import prepare_news_video

        video, metadata = prepare_news_video(store, job, settings, folder)
        store.update(job["id"], title=metadata.title)
    else:
        video, metadata = _prepare_story(store, job, folder, settings)
    if job["publish"]:
        store.update(job["id"], stage="upload")
        video_id = publish_video(store, job, video, metadata, folder)
        store.update(job["id"], status="uploaded", stage="complete", video_id=video_id)
        store.notify(f"업로드 완료 · {metadata.title}\nhttps://youtu.be/{video_id}", job["id"])
    else:
        store.update(job["id"], status="ready", stage="complete")
        store.notify(f"영상 완성 · {metadata.title}\n작업: {job['id']}\n/preview {job['id']}", job["id"])


def run_next(root: Path) -> str | None:
    store = Store(root)
    with exclusive_lock(root / "worker.lock"):
        InstagramStore(store).reconcile_auth()
        store.recover()
        if store.value("paused", False):
            return None
        job = store.claim()
        if job is None:
            return None
        try:
            execute_job(store, job)
        except InstagramError as exc:
            instagram = InstagramStore(store)
            if exc.code in {"busy", "deferred", "rate_limit", "auth_wait"}:
                instagram.wait_job(job["id"], exc.code, retry_at=exc.retry_at,
                                   auth_generation=exc.auth_generation)
            elif exc.code in {"unsupported", "unavailable"}:
                with store.transaction():
                    instagram.reject_item(job["input"]["item_id"], exc.code)
                    store.update(job["id"], status="blocked", error=str(exc))
                    store.notify(f"소재 확인 필요 · {job['id']}\n{exc}", job["id"])
            else:
                store.fail(job["id"], str(exc), delay=60 * 2 ** (job["attempts"] - 1))
                store.notify(f"작업 재시도/실패 · {job['id']}\n{exc}", job["id"])
        except UnsupportedArticle as exc:
            from .news_store import NewsStore

            with store.transaction():
                NewsStore(store).reject_item(job["input"]["item_id"], str(exc))
                store.update(job["id"], status="blocked", error=str(exc))
                store.notify(f"기사 확인 필요 · {job['id']}\n{exc}", job["id"])
        except BlockedError as exc:
            store.update(job["id"], status="blocked", error=str(exc)[:500])
            store.notify(f"설정/복구 필요 · {job['id']}\n{exc}", job["id"])
        except QuotaError as exc:
            # Quota exhaustion isn't a failed generation attempt; wait without paid fallback.
            store.update(job["id"], attempts=max(0, job["attempts"] - 1))
            store.fail(job["id"], str(exc), delay=6 * 3600)
            store.notify(f"무료 할당량 대기 · {job['id']}\n{exc}", job["id"])
        except Exception as exc:
            message = str(exc)[:400] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            store.fail(job["id"], message, delay=60 * 2 ** (job["attempts"] - 1))
            store.notify(f"작업 재시도/실패 · {job['id']}\n{message}", job["id"])
        return job["id"]
