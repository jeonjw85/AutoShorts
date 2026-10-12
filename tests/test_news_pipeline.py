import hashlib
import json
from types import SimpleNamespace

import pytest

from shorts_local.pipeline import run_next
from shorts_local.store import Store
from tests.news_helpers import article, plan_data
from tests.test_news_render import tones


def setup_job(tmp_path, monkeypatch):
    from shorts_local import editing, news_pipeline, news_plan
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    s.update_profile_settings("default", {"width": 360, "height": 640, "min_seconds": 1})
    job_id = NewsStore(s).import_article("https://example.com/a", publish=False)
    monkeypatch.setattr(news_pipeline, "_fetch_article", lambda *args: article())
    calls = []

    def draft(settings, prompt, folder, *, schema, **kwargs):
        calls.append(schema["title"])
        return json.dumps({"supported": True, "issues": []} if schema["title"] == "GroundingVerdict" else plan_data())

    def speech(args, log, timeout):
        request = json.loads((log.parent / "speech-request.json").read_text())
        tones(log.parent / request["voice_subdir"], len(request["texts"]))

    monkeypatch.setattr(news_plan, "generate_draft", draft)
    monkeypatch.setattr(editing, "run_process", speech)
    monkeypatch.setattr(editing.importlib.util, "find_spec", lambda name: object())
    return s, job_id, calls


def test_sealed_news_video_recovers_without_fetch_or_model(tmp_path, monkeypatch):
    from shorts_local import news_pipeline, news_plan
    from shorts_local.pipeline import execute_job

    s, job_id, calls = setup_job(tmp_path, monkeypatch)
    assert run_next(tmp_path) == job_id
    assert s.get(job_id)["status"] == "ready"
    folder = tmp_path / "jobs" / job_id
    video = (folder / "video.mp4").read_bytes()
    (folder / "article.json").write_text('{"body":"changed after upload"}')
    (folder / "upload-state.json").write_text('{}')

    def forbidden(*args, **kwargs):
        pytest.fail("A sealed upload must not regenerate anything")

    monkeypatch.setattr(news_pipeline, "_fetch_article", forbidden)
    monkeypatch.setattr(news_plan, "generate_draft", forbidden)
    execute_job(s, s.get(job_id))
    assert (folder / "video.mp4").read_bytes() == video
    assert calls == ["NewsPlan", "GroundingVerdict"]
    assert not (folder / "story.json").exists()
    (folder / "metadata.json").write_text('{}')
    s.retry(job_id)
    run_next(tmp_path)
    assert s.get(job_id)["status"] == "blocked"
    assert (folder / "video.mp4").read_bytes() == video


@pytest.mark.parametrize("failure", ["disk", "short", "ungrounded"])
def test_news_failure_blocks_before_render(tmp_path, monkeypatch, failure):
    from shorts_local import news_pipeline, news_plan
    from shorts_local.news_fetch import UnsupportedArticle
    from shorts_local.news_store import NewsStore

    s, job_id, calls = setup_job(tmp_path, monkeypatch)
    if failure == "disk":
        monkeypatch.setattr(news_pipeline.shutil, "disk_usage", lambda p: SimpleNamespace(free=1))
        monkeypatch.setattr(news_pipeline, "_fetch_article", lambda *a: pytest.fail("download on low disk"))
    elif failure == "short":
        def short(*args):
            raise UnsupportedArticle("본문이 너무 짧습니다.")
        monkeypatch.setattr(news_pipeline, "_fetch_article", short)
    else:
        def bad(*args, **kwargs):
            calls.append("bad")
            data = plan_data()
            data["cards"][0]["narration"] = "서울시는 버스 999대를 도입했다고 밝혔습니다."
            return json.dumps(data)
        monkeypatch.setattr(news_plan, "generate_draft", bad)
    run_next(tmp_path)
    assert s.get(job_id)["status"] == "blocked"
    assert not (tmp_path / "jobs" / job_id / "video.mp4").exists()
    if failure == "short":
        assert NewsStore(s).materials()[0]["status"] == "unsupported"
        assert NewsStore(s).enqueue_next() is None
    if failure == "ungrounded":
        assert calls == ["bad", "bad"]


def test_changed_article_does_not_reuse_plan_after_interrupted_render(tmp_path, monkeypatch):
    from shorts_local import news_pipeline

    s, job_id, calls = setup_job(tmp_path, monkeypatch)
    render = news_pipeline.render_news_video

    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted render")

    monkeypatch.setattr(news_pipeline, "render_news_video", interrupted)
    run_next(tmp_path)
    assert s.get(job_id)["status"] == "retry"
    folder = tmp_path / "jobs" / job_id
    cache = json.loads((folder / "article.json").read_text())
    cache["data"]["body"] += " 시민들은 새 노선을 기다리고 있습니다."
    cache["data"]["body_sha256"] = hashlib.sha256(cache["data"]["body"].encode()).hexdigest()
    (folder / "article.json").write_text(json.dumps(cache))
    monkeypatch.setattr(news_pipeline, "render_news_video", render)
    s.retry(job_id)
    run_next(tmp_path)
    assert s.get(job_id)["status"] == "ready"
    assert calls == ["NewsPlan", "GroundingVerdict"] * 2


def test_temporary_dns_failure_uses_bounded_job_retries(tmp_path, monkeypatch):
    import socket
    import sys

    from shorts_local import news_fetch, news_pipeline
    from shorts_local.news_store import NewsStore

    s = Store(tmp_path)
    job_id = NewsStore(s).import_article("https://example.com/a")

    def resolve(*args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary resolver failure")

    def worker(args, log, timeout):
        # Keep the actual worker error serialization and parent/queue handling.
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", ["news_fetch", args[-1]])
            news_fetch.main()

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(news_pipeline, "run_process", worker)
    for attempts, expected in ((1, "retry"), (2, "retry"), (3, "failed")):
        s.update(job_id, next_run=0)
        run_next(tmp_path)
        assert s.get(job_id)["status"] == expected
        assert s.get(job_id)["attempts"] == attempts
        assert NewsStore(s).materials()[0]["status"] == "reserved"
