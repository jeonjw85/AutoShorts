import json

import pytest
from typer.testing import CliRunner

from shorts_local.cli import app
from shorts_local.instagram_store import InstagramStore
from shorts_local.store import Store
from shorts_local.telegram import handle_update
from tests.instagram_helpers import batch, post
from tests.test_service import update


def test_cli_import_create_and_flag_conflicts(tmp_path, story_data):
    runner, home = CliRunner(), ["--home", str(tmp_path)]
    assert runner.invoke(app, home + ["init"]).exit_code == 0
    assert runner.invoke(app, home + ["channel", "add", "animals", "--name", "동물"]).exit_code == 0
    assert runner.invoke(app, home + ["config", "--channel", "animals", "production_type", "instagram"]).exit_code == 0
    result = runner.invoke(app, home + ["import-video", post().url, "--channel", "animals", "--mode", "original", "--local-only"])
    assert result.exit_code == 0, result.output
    assert "instagram/original" in result.output
    store = Store(tmp_path)
    job = store.list()[0]
    assert job["channel_id"] == "animals" and not job["publish"]
    assert job["settings"]["instagram_edit_mode"] == "original"
    empty = runner.invoke(app, home + ["create", "--channel", "animals"])
    assert empty.exit_code == 0 and "소재" in empty.output and len(store.list()) == 1
    document = tmp_path / "example.json"
    document.write_text(json.dumps(story_data))
    before = store.profile("animals").model_dump()
    for arguments in (["--type", "instagram", "--story", str(document)], ["--type", "instagram", "--topic", "새 주제"],
                      ["--type", "story", "--mode", "original"], ["--upload", "--local-only"], ["--mode", "wrong"]):
        result = runner.invoke(app, home + ["create", "--channel", "animals", *arguments])
        assert result.exit_code != 0, arguments
        assert len(store.list()) == 1
    assert runner.invoke(app, home + ["import-video", post("Other").url, "--upload", "--local-only"]).exit_code != 0
    assert len(store.list()) == 1 and store.profile("animals").model_dump() == before
    assert runner.invoke(app, home + ["create", "--channel", "animals", "--story", str(document)]).exit_code == 0
    assert store.list()[0]["settings"]["production_type"] == "story"


@pytest.mark.parametrize("text", [post().url + "?igsh=tracking", "/import " + post().url + " original"])
def test_telegram_import_is_atomic_and_replay_safe(tmp_path, monkeypatch, text):
    store = Store(tmp_path)
    store.add_profile("animals", "동물")
    original = store.accept_telegram
    monkeypatch.setattr(store, "accept_telegram", lambda *a: (_ for _ in ()).throw(RuntimeError("ack fails")))
    with pytest.raises(RuntimeError):
        handle_update(store, update(text, update_id=100), 42)
    assert store.list() == [] and InstagramStore(store).materials() == []
    assert store.pending_notifications() == [] and store.value("telegram_offset") is None
    monkeypatch.setattr(store, "accept_telegram", original)
    reply = handle_update(store, update(text, update_id=100), 42)
    assert "instagram/" in reply["text"]
    store.set_value("telegram_channel", "animals")
    assert handle_update(store, update(text, update_id=100), 42) == reply
    assert len(store.list()) == 1 and store.list()[0]["channel_id"] == "default"


def test_source_scan_and_login_do_not_run_inside_command_transaction(tmp_path, monkeypatch):
    import shorts_local.collector as collector
    import shorts_local.instagram as instagram

    store = Store(tmp_path)
    calls = []
    def collect(root, **kwargs):
        with Store(root).transaction():
            calls.append(kwargs["channel_id"])
        return None
    monkeypatch.setattr(collector, "run_collection", collect)
    reply = handle_update(store, update("/source_add @pets", update_id=100), 42)
    assert "pending" in reply["text"]
    handle_update(store, update("/source_scan", update_id=101), 42)
    assert calls == []
    runner = CliRunner()
    home = ["--home", str(tmp_path)]
    assert runner.invoke(app, home + ["source", "scan"]).exit_code == 0
    assert calls == ["default"]
    def login(store, username, *, prompt_secret):
        assert prompt_secret("비밀번호") == "hidden-secret"
        with Store(store.root).transaction():
            pass
        return username
    monkeypatch.setattr(instagram, "login_instagram", login)
    result = runner.invoke(app, home + ["instagram-login", "pets"], input="hidden-secret\n")
    assert result.exit_code == 0, result.output
    assert "hidden-secret" not in result.output


def test_cross_channel_ids_status_and_unauthorized_urls(tmp_path):
    store = Store(tmp_path)
    ig = InstagramStore(store)
    source = ig.add_source("pets", now=100)
    ig.apply_scan(batch(source, [post()]))
    job = ig.enqueue_next(mode="original")
    store.update(job, status="auth_wait")
    store.set_value("instagram_auth_wait", True)
    store.add_profile("other", "다른 채널")
    store.set_value("telegram_channel", "other")
    reply = handle_update(store, update(f"/source_pause {source['id']}", update_id=100), 42)
    assert store.profile().label in reply["text"] and not ig.source(source["id"])["active"]
    assert store.profile().label in handle_update(store, update(f"/retry {job}", update_id=101), 42)["text"]
    for msg in (update(post().url, owner=99), update(post().url, chat_type="group")):
        assert handle_update(store, msg, 42) is None
    assert len(store.list()) == 1
    handle_update(store, update("/set type instagram", update_id=102), 42)
    reply = handle_update(store, update("/status", update_id=103), 42)
    assert "소재 대기" in reply["text"] and "auth_wait" in reply["text"]
    handle_update(store, update("/channel default", update_id=104), 42)
    assert job in handle_update(store, update("/materials", update_id=105), 42)["text"]
    reply = handle_update(store, update("/create_story 새 이야기", update_id=106), 42)
    assert "story" in reply["text"] and len(store.list()) == 2


def test_doctor_and_model_download_selection(tmp_path, monkeypatch):
    import huggingface_hub

    import shorts_local.cli as cli

    store = Store(tmp_path)
    repos = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **kw: repos.append(repo))
    runner, home = CliRunner(), ["--home", str(tmp_path)]
    result = runner.invoke(app, home + ["models", "--instagram", "--images"])
    assert result.exit_code == 0, result.output
    settings = store.profile().settings
    assert repos == [settings.text_model, settings.speech_model, settings.image_model,
                     settings.transcription_model, settings.vision_model]
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, home + ["doctor"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert not report["instagram"]["instaloader_installed"]
    assert not report["instagram"]["session_present"]
    assert len(repos) == 5
