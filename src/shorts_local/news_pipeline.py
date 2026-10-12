"""Recoverable article production with sealed publication artifacts."""

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .artifacts import artifact_key, read_artifact, write_artifact
from .config import Settings
from .editing import prepare_speech
from .models import VideoMetadata
from .news_fetch import ArticleDocument, UnsupportedArticle
from .news_models import NewsInput
from .news_plan import plan_news
from .news_render import render_news_video
from .process import BlockedError, atomic_json, run_process
from .render import validate_video
from .store import Store
from .youtube import file_hash


def _job_key(job, settings):
    return artifact_key("news-job", {"input": job["input"], "settings": settings.model_dump(),
                                     "channel_id": job["channel_id"]})


def _check_input(job, folder):
    value = NewsInput.model_validate(job["input"])
    path = folder / "input.json"
    if path.exists() and NewsInput.model_validate_json(path.read_text()) != value:
        raise ValueError("기사 입력 파일과 작업 스냅샷이 다릅니다.")
    return value


def ready_news_video(job: dict, settings: Settings, folder: Path):
    has_upload = any((folder / name).exists() for name in ("upload-state.json", "upload-receipt.json"))
    try:
        _check_input(job, folder)
        seal = json.loads((folder / "validation.json").read_text())
        video, metadata = folder / "video.mp4", folder / "metadata.json"
        if (seal["schema"] != 1 or seal["kind"] != "news-final" or seal["job_key"] != _job_key(job, settings)
                or seal["video_sha256"] != file_hash(video) or seal["metadata_sha256"] != file_hash(metadata)):
            raise ValueError("뉴스 완성 기록과 파일이 다릅니다.")
        timeline = folder / "timeline.json"
        if timeline.exists() and file_hash(timeline) != seal["timeline_sha256"]:
            raise ValueError("뉴스 타임라인이 변경되었습니다.")
        value = VideoMetadata.model_validate_json(metadata.read_text())
        validate_video(video, settings)
        return video, value
    except (OSError, ValueError, KeyError, TypeError):
        if has_upload:
            raise BlockedError("업로드가 시작된 뉴스 영상의 완성 기록/파일이 다릅니다. 복구 확인이 필요합니다.") from None
        return None


def _fetch_article(value: NewsInput, folder: Path) -> ArticleDocument:
    with tempfile.TemporaryDirectory(prefix="article-", dir=folder) as directory:
        temp = Path(directory)
        atomic_json(temp / "request.json", value.model_dump())
        try:
            run_process([sys.executable, "-m", "shorts_local.news_fetch", str(temp)], temp / "fetch.log", 30)
        except RuntimeError:
            if not (temp / "article.json").exists():
                raise RuntimeError("기사 조회 시간이 초과되었습니다.") from None
        if not (temp / "article.json").exists():
            error = json.loads((temp / "error.json").read_text())
            if error["unsupported"]:
                raise UnsupportedArticle("기사 본문/날짜/공개 주소를 확인할 수 없습니다.")
            raise RuntimeError("기사 서버에 연결하지 못했습니다.")
        article = ArticleDocument.model_validate_json((temp / "article.json").read_text())
        if (temp / "image.jpg").exists():
            article.image_data = (temp / "image.jpg").read_bytes()
        return article


def prepare_news_video(store: Store, job: dict, settings: Settings,
                        folder: Path) -> tuple[Path, VideoMetadata]:
    if ready := ready_news_video(job, settings, folder):
        return ready
    try:
        value = _check_input(job, folder)
    except (OSError, ValueError):
        raise BlockedError("기사 입력 파일과 작업 스냅샷이 다릅니다. 입력 복구가 필요합니다.") from None
    if shutil.disk_usage(folder).free < 5 * 1024**3:
        raise BlockedError("디스크 여유 공간이 5GiB 미만입니다. 공간 확보 후 재시도하세요.")
    atomic_json(folder / "input.json", value.model_dump())
    key = _job_key(job, settings)
    store.update(job["id"], stage="fetch")
    saved = read_artifact(folder / "article.json", key)
    article = None
    if saved:
        try:
            article = ArticleDocument.model_validate(saved)
        except ValueError:
            pass
    if article is None:
        article = _fetch_article(value, folder)
        image = folder / "image.jpg"
        image.unlink(missing_ok=True)
        if article.image_data:
            image.write_bytes(article.image_data)
            write_artifact(folder / "news-image.json", article.body_sha256, {"sha256": file_hash(image)})
        store.update(job["id"], stage="extract")
        write_artifact(folder / "article.json", key, article.model_dump())
    store.update(job["id"], stage="plan")
    plan = plan_news(settings, article, folder)
    store.update(job["id"], title=plan.title,
                 fingerprint=hashlib.sha256(f"news:{value.url}".encode()).hexdigest())
    store.update(job["id"], stage="speech")
    voices = prepare_speech(settings, [card.narration for card in plan.cards], folder)
    store.update(job["id"], stage="render")
    image = folder / "image.jpg"
    manifest = read_artifact(folder / "news-image.json", article.body_sha256)
    image_ok = image.exists() and manifest and manifest.get("sha256") == file_hash(image)
    video = render_news_video(plan, article, voices, folder, settings, image=image if image_ok else None)
    metadata = VideoMetadata(title=plan.title, description=plan.description)
    atomic_json(folder / "metadata.json", metadata.model_dump())
    report = validate_video(video, settings)
    atomic_json(folder / "validation.json", {
        "schema": 1, "kind": "news-final", "job_key": key,
        "article_sha256": article.body_sha256, "plan_key": artifact_key("plan", plan.model_dump()),
        "video_sha256": file_hash(video), "metadata_sha256": file_hash(folder / "metadata.json"),
        "timeline_sha256": file_hash(folder / "timeline.json"), "media": report,
    })
    return video, metadata
