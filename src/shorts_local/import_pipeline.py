"""Resumable external-video production; publishing stays in the common pipeline."""

import hashlib
import json
import shutil
from pathlib import Path

from .analysis import analyze_source
from .artifacts import artifact_key, read_artifact
from .config import Settings
from .editing import plan_edit, prepare_narration
from .editing_models import EditPlan
from .import_render import NarrationTimingError, minimum_duration, render_imported_video
from .instagram import fetch_source
from .instagram_models import InstagramInput
from .instagram_store import InstagramStore
from .models import VideoMetadata
from .process import BlockedError, atomic_json
from .render import validate_video
from .store import Store
from .youtube import file_hash


def _job_key(job, settings):
    # Schema-1 Instagram seals predate news-only settings. Those defaults must
    # neither invalidate old upload bytes nor influence new Instagram seals.
    parameters = settings.model_dump(exclude={"news_style", "news_poll_minutes"})
    return artifact_key("instagram-job", {"input": job["input"], "settings": parameters,
                                          "channel_id": job["channel_id"]})


def _check_input(job, folder):
    value = InstagramInput.model_validate(job["input"])
    if (folder / "input.json").exists():
        saved = InstagramInput.model_validate_json((folder / "input.json").read_text())
        if saved != value:
            raise ValueError("원본 입력 파일과 작업 스냅샷이 다릅니다.")
    return value


def ready_imported_video(job: dict, settings: Settings, workdir: Path) -> tuple[Path, VideoMetadata] | None:
    has_upload = any((workdir / name).exists() for name in ("upload-state.json", "upload-receipt.json"))
    try:
        _check_input(job, workdir)
        seal = json.loads((workdir / "validation.json").read_text())
        video, metadata_path = workdir / "video.mp4", workdir / "metadata.json"
        if (seal["schema"] != 1 or seal["kind"] != "instagram-final"
                or seal["job_key"] != _job_key(job, settings)
                or seal["video_sha256"] != file_hash(video)
                or seal["metadata_sha256"] != file_hash(metadata_path)):
            raise ValueError("완성 기록과 파일이 다릅니다.")
        # Publication needs the sealed MP4 and metadata, not intermediate editing
        # diagnostics. If diagnostics are retained, they must still match.
        timeline = workdir / "timeline.json"
        if timeline.exists() and file_hash(timeline) != seal["timeline_sha256"]:
            raise ValueError("완성 타임라인이 변경되었습니다.")
        minimum = float(seal["min_seconds"])
        if not 0 < minimum <= settings.min_seconds:
            raise ValueError("완성 기록의 최소 길이가 올바르지 않습니다.")
        metadata = VideoMetadata.model_validate_json(metadata_path.read_text())
        validate_video(video, settings, min_seconds=minimum)
        return video, metadata
    except (OSError, ValueError, KeyError, TypeError):
        if has_upload:
            raise BlockedError("업로드가 시작된 영상의 완성 기록/파일이 다릅니다. 재렌더 없이 복구 확인이 필요합니다.") from None
        return None


def prepare_imported_video(store: Store, job: dict, settings: Settings,
                           workdir: Path) -> tuple[Path, VideoMetadata]:
    if ready := ready_imported_video(job, settings, workdir):
        return ready
    try:
        input = _check_input(job, workdir)
    except (OSError, ValueError):
        raise BlockedError("원본 입력 파일과 작업 스냅샷이 다릅니다. 입력 복구가 필요합니다.") from None
    if shutil.disk_usage(workdir).free < 5 * 1024**3:
        raise BlockedError("디스크 여유 공간이 5GiB 미만입니다. 공간 확보 후 재시도하세요.")
    atomic_json(workdir / "input.json", input.model_dump())
    store.update(job["id"], stage="fetch")
    source = fetch_source(store.root, input, workdir, settings, channel_id=job["channel_id"])
    InstagramStore(store).record_media(input.item_id, source, job_id=job["id"])
    store.update(job["id"], stage="analyze")
    analysis = analyze_source(source, workdir, settings)
    timing_key = artifact_key("timing", {"source": source.model_dump(), "analysis": analysis.model_dump(),
                                         "settings": settings.model_dump()})
    feedback = read_artifact(workdir / "timing-feedback.json", timing_key)
    previous_plan = None
    if feedback:
        try:
            previous_plan = EditPlan.model_validate(feedback["initial_plan"])
        except (KeyError, ValueError):
            raise BlockedError("해설 길이 보정의 최초 편집 계획이 없습니다. 작업 파일 복구가 필요합니다.") from None
    while True:
        store.update(job["id"], stage="edit-plan")
        plan = plan_edit(settings, source, analysis, workdir,
                         timing_feedback=feedback["durations"] if feedback else None, previous_plan=previous_plan)
        store.update(job["id"], title=plan.title,
                     fingerprint=hashlib.sha256(f"instagram:{input.shortcode}".encode()).hexdigest())
        store.update(job["id"], stage="speech" if plan.mode == "narrated" else "render")
        voices = prepare_narration(settings, plan, workdir)
        store.update(job["id"], stage="render")
        try:
            video = render_imported_video(source, plan, analysis, workdir, settings, voices)
            break
        except NarrationTimingError as exc:
            if feedback:
                raise BlockedError("한 번 보정한 해설도 길이/속도 조건에 맞지 않습니다. 편집 결과를 확인하세요.") from None
            feedback = {"attempt": 1, "initial_plan_key": artifact_key("plan", plan.model_dump()),
                        "initial_plan": plan.model_dump(), "durations": exc.durations}
            atomic_json(workdir / "timing-feedback.json", {"schema": 1, "key": timing_key, "data": feedback})
            previous_plan = plan
    metadata = VideoMetadata(title=plan.title, description=plan.description)
    atomic_json(workdir / "metadata.json", metadata.model_dump())
    report = validate_video(video, settings, min_seconds=minimum_duration(source, settings))
    # The final seal is last: a crash during any preceding replace is rebuildable
    # only before an upload state/receipt exists.
    atomic_json(workdir / "validation.json", {
        "schema": 1, "kind": "instagram-final", "job_key": _job_key(job, settings),
        "source_sha256": source.sha256, "plan_key": artifact_key("plan", plan.model_dump()),
        "video_sha256": file_hash(video), "metadata_sha256": file_hash(workdir / "metadata.json"),
        "timeline_sha256": file_hash(workdir / "timeline.json"),
        "min_seconds": minimum_duration(source, settings), "media": report,
    })
    return video, metadata
