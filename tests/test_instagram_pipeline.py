import json
import shutil

import pytest

from shorts_local.config import Settings
from shorts_local.instagram import InstagramError
from shorts_local.instagram_store import InstagramStore
from shorts_local.process import BlockedError, atomic_json
from shorts_local.store import Store
from shorts_local.youtube import file_hash
from tests.instagram_helpers import make_source, post
from tests.test_channels_integration import connect, prepare_media, youtube_network
from tests.test_instagram_render import edit
from tests.test_media import write_tone
from tests.test_youtube import A


def prepared_job(root, monkeypatch, *, mode="original", channel="default", publish=False):
    import shorts_local.import_pipeline as imported

    store = Store(root)
    store.update_profile_settings(channel, {"width": 360, "height": 640,
                                            "instagram_edit_mode": mode, "instagram_style": "등록 당시 스타일"})
    job = InstagramStore(store).import_video(post().url, channel_id=channel, publish=publish)
    folder = root / "jobs" / job
    source = make_source(folder)
    atomic_json(folder / "source.json", source.model_dump())
    plan, analysis = edit(mode)
    calls = []

    def planner(settings, value, analysis, workdir, **kwargs):
        calls.append((settings.instagram_edit_mode, settings.instagram_style))
        return plan

    monkeypatch.setattr(imported, "analyze_source", lambda *args: analysis)
    monkeypatch.setattr(imported, "plan_edit", planner)
    return store, job, folder, source, calls


def test_external_job_keeps_channel_and_mode_snapshot(tmp_path, monkeypatch):
    import shorts_local.pipeline as pipeline

    store = Store(tmp_path)
    store.add_profile("animals", "동물")
    connect(store, "animals", A)
    transfers = youtube_network(monkeypatch)
    store, job, folder, source, calls = prepared_job(tmp_path, monkeypatch, channel="animals", publish=True)
    store.update_profile_settings("animals", {"instagram_edit_mode": "narrated", "instagram_style": "바뀐 스타일"})
    store.set_value("telegram_channel", "default")
    monkeypatch.setattr(pipeline, "plan_story", lambda *a: pytest.fail("story prompt must not run"))
    monkeypatch.setattr(pipeline, "engine", lambda *a: pytest.fail("image/story speech must not run"))
    assert pipeline.run_next(tmp_path) == job
    assert store.get(job)["status"] == "uploaded", store.get(job)["error"]
    assert calls == [("original", "등록 당시 스타일")]
    assert transfers == [("POST", A), ("PUT", A)]
    assert not (folder / "story.json").exists()


def test_ready_video_recovers_without_source_models_or_disk_budget(tmp_path, monkeypatch):
    import shorts_local.import_pipeline as imported
    import shorts_local.pipeline as pipeline

    store, job, folder, source, calls = prepared_job(tmp_path, monkeypatch)
    connect(store, "default", A)
    store.bind_job_target(job, A)
    pipeline.run_next(tmp_path)
    assert store.get(job)["status"] == "ready", store.get(job)["error"]
    keep = {"video.mp4", "metadata.json", "validation.json"}
    for path in folder.iterdir():
        if path.name not in keep:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    atomic_json(folder / "upload-receipt.json", {"sha256": file_hash(folder / "video.mp4"),
                "video_id": "video123456", "channel_id": "default", "youtube_channel_id": A})
    store.update(job, publish=True)
    store.retry(job)
    def forbidden(*args, **kwargs):
        pytest.fail("completed publication must not need production resources")
    for function in ("fetch_source", "analyze_source", "plan_edit", "prepare_narration", "render_imported_video"):
        monkeypatch.setattr(imported, function, forbidden)
    monkeypatch.setattr(shutil, "disk_usage", forbidden)
    monkeypatch.setattr(pipeline, "read_auth", forbidden)
    pipeline.run_next(tmp_path)
    assert store.get(job)["status"] == "uploaded", store.get(job)["error"]


@pytest.mark.parametrize("file", ["video.mp4", "metadata.json", "validation.json", "timeline.json"])
def test_upload_artifact_prevents_rerender_of_changed_video(tmp_path, monkeypatch, file):
    import shorts_local.import_pipeline as imported
    import shorts_local.pipeline as pipeline

    store, job, folder, source, calls = prepared_job(tmp_path, monkeypatch)
    pipeline.run_next(tmp_path)
    assert store.get(job)["status"] == "ready", store.get(job)["error"]
    (folder / file).write_bytes(b"changed")
    atomic_json(folder / "upload-state.json", {"existing": True})
    monkeypatch.setattr(imported, "render_imported_video", lambda *a: pytest.fail("must not re-render"))
    with pytest.raises(BlockedError):
        imported.ready_imported_video(store.get(job), Settings.model_validate(store.get(job)["settings"]), folder)


def test_stage_hashes_reject_stale_or_partial_artifacts(tmp_path, monkeypatch):
    import shorts_local.import_pipeline as imported
    import shorts_local.pipeline as pipeline

    store, job, folder, source, calls = prepared_job(tmp_path, monkeypatch)
    pipeline.run_next(tmp_path)
    settings = Settings.model_validate(store.get(job)["settings"])
    assert imported.ready_imported_video(store.get(job), settings, folder)
    (folder / "video.partial.mp4").write_bytes(b"incomplete")
    assert imported.ready_imported_video(store.get(job), settings, folder)
    (folder / "metadata.json").write_text('{"title":"다른 제목","description":"다른 메타데이터입니다."}')
    assert imported.ready_imported_video(store.get(job), settings, folder) is None
    store.retry(job)
    pipeline.run_next(tmp_path)
    assert store.get(job)["status"] == "ready"
    seal = json.loads((folder / "validation.json").read_text())
    assert seal["source_sha256"] == source.sha256
    assert seal["video_sha256"] == file_hash(folder / "video.mp4")


def test_timing_replan_is_once_even_across_restart(tmp_path, monkeypatch):
    import shorts_local.editing as editing
    import shorts_local.import_pipeline as imported

    store, job, folder, source, calls = prepared_job(tmp_path, monkeypatch, mode="narrated")
    monkeypatch.setattr(imported, "plan_edit", editing.plan_edit)
    drafts = []
    def generate(settings, prompt, workdir, **kwargs):
        drafts.append(prompt)
        plan, analysis = edit("narrated")
        return plan.model_dump_json()
    monkeypatch.setattr(editing, "generate_draft", generate)
    voice = folder / "voice.wav"
    write_tone(voice, 1)
    monkeypatch.setattr(imported, "prepare_narration", lambda *a: [voice])
    original = imported.atomic_json
    def interrupted(path, data):
        original(path, data)
        if path.name == "timing-feedback.json":
            raise KeyboardInterrupt
    monkeypatch.setattr(imported, "atomic_json", interrupted)
    settings = Settings.model_validate(store.get(job)["settings"])
    with pytest.raises(KeyboardInterrupt):
        imported.prepare_imported_video(store, store.get(job), settings, folder)
    feedback = json.loads((folder / "timing-feedback.json").read_text())["data"]
    assert feedback["initial_plan"]["clips"][0]["narration"] == "원본 화면의 움직임을 살펴보세요."
    monkeypatch.setattr(imported, "atomic_json", original)
    for _ in range(2):
        with pytest.raises(BlockedError, match="길이"):
            imported.prepare_imported_video(store, store.get(job), settings, folder)
    assert len(drafts) == 2


@pytest.mark.parametrize("code", ["rate_limit", "busy", "auth_wait", "transient", "unsupported", "unavailable"])
def test_import_waits_do_not_block_other_channels(tmp_path, monkeypatch, story_data, code):
    import shorts_local.import_pipeline as imported
    import shorts_local.pipeline as pipeline

    store = Store(tmp_path)
    store.add_profile("other", "다른 채널")
    job = InstagramStore(store).import_video(post().url)
    other = store.enqueue(Settings(width=360, height=640, min_seconds=1), channel_id="other")
    prepare_media(store, other, story_data)
    def unavailable(*args):
        raise InstagramError(code)
    monkeypatch.setattr(imported, "fetch_source", unavailable)
    assert pipeline.run_next(tmp_path) == job
    expected = {"auth_wait": "auth_wait", "unsupported": "blocked", "unavailable": "blocked"}.get(code, "retry")
    assert store.get(job)["status"] == expected, store.get(job)["error"]
    assert store.get(job)["attempts"] == (0 if code in {"rate_limit", "busy", "auth_wait"} else 1)
    assert pipeline.run_next(tmp_path) == other
    assert store.get(other)["status"] == "ready"
    if code == "auth_wait":
        store.retry(job)
        assert store.get(job)["manual_run"] == 1
    if code == "transient":
        for _ in range(2):
            store.update(job, next_run=0)
            pipeline.run_next(tmp_path)
        assert store.get(job)["status"] == "failed"
