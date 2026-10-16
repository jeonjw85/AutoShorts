import hashlib
import json
from types import SimpleNamespace

import pytest

from shorts_local import pipeline, render
from shorts_local.config import Settings
from shorts_local.models import Story
from shorts_local.process import atomic_json
from shorts_local.store import Store
from tests.test_media import write_tone


@pytest.fixture
def story_job(tmp_path, story_data, monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _: SimpleNamespace(free=40 * 1024**3))
    store = Store(tmp_path)
    settings = Settings(width=360, height=640, min_seconds=1)
    job_id = store.enqueue(settings, story=Story.model_validate(story_data), publish=False)
    folder = tmp_path / "jobs" / job_id
    (folder / "voice").mkdir()
    for index in range(3):
        write_tone(folder / "voice" / f"{index:03}.wav")
    return store, job_id, folder, settings


def erase_media_payload(path):
    payload = bytearray(path.read_bytes())
    offset = 0
    while offset < len(payload):
        size = int.from_bytes(payload[offset:offset + 4], "big")
        kind = payload[offset + 4:offset + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(payload[offset + 8:offset + 16], "big")
            header = 16
        if size == 0:
            size = len(payload) - offset
        if kind == b"mdat":
            payload[offset + header:offset + size] = bytes(size - header)
            path.write_bytes(payload)
            return
        offset += size
    raise AssertionError("Fixture has no MP4 media payload")


def finish_story(story_job):
    store, job_id, folder, settings = story_job
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "ready", store.get(job_id)["error"]
    return store, job_id, folder, settings


def test_story_renderer_rejects_undecodable_output(story_job, monkeypatch):
    _, _, folder, settings = story_job
    original = render.run_process

    def corrupt_final_encode(args, *positional, **kwargs):
        original(args, *positional, **kwargs)
        if args[-1] == str(folder / "video.partial.mp4"):
            erase_media_payload(folder / "video.partial.mp4")

    monkeypatch.setattr(render, "run_process", corrupt_final_encode)
    story = Story.model_validate_json((folder / "story.json").read_text())
    with pytest.raises(RuntimeError):
        render.render_video(story, folder, settings)
    assert not (folder / "video.mp4").exists()


@pytest.mark.parametrize("unavailable", ["voice", "disk"])
def test_finished_story_reuses_video_without_generation_resources(story_job, monkeypatch, unavailable):
    store, job_id, folder, _ = finish_story(story_job)
    video = folder / "video.mp4"
    before = video.read_bytes(), video.stat().st_mtime_ns
    if unavailable == "voice":
        for path in (folder / "voice").glob("*.wav"):
            path.unlink()
    else:
        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))

    def forbidden(*args):
        pytest.fail("A finished story must not invoke generation")

    monkeypatch.setattr(pipeline, "engine", forbidden)
    monkeypatch.setattr(pipeline, "plan_story", forbidden)
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "ready", store.get(job_id)["error"]
    assert (video.read_bytes(), video.stat().st_mtime_ns) == before


@pytest.mark.parametrize("changed", ["video", "story", "timeline", "seal", "missing-video"])
def test_story_upload_never_rebuilds_changed_final_artifacts(story_job, changed):
    store, job_id, folder, _ = finish_story(story_job)
    video = folder / "video.mp4"
    atomic_json(folder / "upload-state.json", {
        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "size": video.stat().st_size,
    })
    if changed == "video":
        erase_media_payload(video)
    elif changed == "story":
        story = json.loads((folder / "story.json").read_text())
        atomic_json(folder / "story.json", story | {"title": "바뀐 영상 제목"})
    elif changed == "timeline":
        atomic_json(folder / "timeline.json", [])
    elif changed == "seal":
        (folder / "validation.json").write_text("not-json")
    else:
        video.unlink()
    retained = {name: (folder / name).read_bytes() for name in (
        "story.json", "timeline.json", "validation.json", "upload-state.json")}
    before = video.read_bytes() if video.exists() else None
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "blocked", store.get(job_id)["error"]
    assert (video.read_bytes() if video.exists() else None) == before
    assert {name: (folder / name).read_bytes() for name in retained} == retained


@pytest.mark.parametrize("recorded_upload", [False, True])
def test_legacy_story_video_is_verified_without_regenerating_speech(story_job, monkeypatch, recorded_upload):
    store, job_id, folder, settings = finish_story(story_job)
    video = folder / "video.mp4"
    before = video.read_bytes()
    atomic_json(folder / "validation.json", render.validate_video(video, settings))
    if recorded_upload:
        atomic_json(folder / "upload-state.json", {
            "sha256": hashlib.sha256(before).hexdigest(), "size": len(before),
        })
    for path in (folder / "voice").glob("*.wav"):
        path.unlink()
    (folder / "timeline.json").unlink()
    monkeypatch.setattr(pipeline, "engine", lambda *a: pytest.fail("Legacy recovery invoked speech"))
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "ready", store.get(job_id)["error"]
    assert video.read_bytes() == before
    seal = json.loads((folder / "validation.json").read_text())
    assert seal["video_sha256"] == hashlib.sha256(before).hexdigest()


def test_legacy_story_upload_hash_mismatch_is_blocked(story_job):
    store, job_id, folder, settings = finish_story(story_job)
    video = folder / "video.mp4"
    atomic_json(folder / "validation.json", render.validate_video(video, settings))
    atomic_json(folder / "upload-state.json", {"sha256": "0" * 64, "size": video.stat().st_size})
    before = video.read_bytes(), (folder / "validation.json").read_bytes()
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "blocked"
    assert (video.read_bytes(), (folder / "validation.json").read_bytes()) == before


@pytest.mark.parametrize("missing_video", [False, True])
def test_completed_story_rejects_changed_script_before_upload(story_job, missing_video):
    store, job_id, folder, _ = finish_story(story_job)
    story = json.loads((folder / "story.json").read_text())
    story["scenes"][0]["narration"] = "완성한 영상과 다른 문장으로 바꾸면 음성이 달라져야 합니다."
    atomic_json(folder / "story.json", story)
    video = folder / "video.mp4"
    if missing_video:
        video.unlink()
    before = video.read_bytes() if video.exists() else None
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "blocked"
    assert (video.read_bytes() if video.exists() else None) == before


def test_corrupt_local_story_is_rebuilt_before_publication(story_job):
    store, job_id, folder, settings = finish_story(story_job)
    video = folder / "video.mp4"
    erase_media_payload(video)
    corrupted = video.read_bytes()
    store.retry(job_id)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "ready", store.get(job_id)["error"]
    assert video.read_bytes() != corrupted
    render.validate_video(video, settings, full_decode=True)


def test_finished_story_recovers_upload_receipt_without_models(story_job, monkeypatch):
    from tests.test_youtube import A

    store, job_id, folder, _ = finish_story(story_job)
    video = folder / "video.mp4"
    store.bind_channel("default", A, "원래 채널")
    store.bind_job_target(job_id, A)
    atomic_json(folder / "upload-receipt.json", {
        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "video_id": "video123456",
        "channel_id": "default", "youtube_channel_id": A,
    })
    for path in (folder / "voice").glob("*.wav"):
        path.unlink()
    monkeypatch.setattr(pipeline, "engine", lambda *a: pytest.fail("Receipt recovery invoked speech"))
    store.retry(job_id)
    store.update(job_id, publish=True)
    pipeline.run_next(store.root)
    assert store.get(job_id)["status"] == "uploaded", store.get(job_id)["error"]
    assert store.get(job_id)["video_id"] == "video123456"
