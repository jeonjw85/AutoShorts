import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shorts_local.cli import app
from shorts_local.process import exclusive_lock
from shorts_local.store import Store
from tests.test_service import update


def project_version():
    return tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"},
                   capture_output=True, text=True)


def checkout(path: Path, remote: str = "https://github.com/jeonjw85/AutoShorts.git") -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "remote", "add", "origin", remote)
    (path / "pyproject.toml").write_text('name = "shorts-local"\n')
    git(path, "add", "pyproject.toml")
    git(path, "commit", "-m", "init")
    return path


def test_version_is_shown_by_cli_doctor_and_status(tmp_path):
    version = project_version()
    runner = CliRunner()
    shown = runner.invoke(app, ["--version"])
    assert shown.exit_code == 0
    assert shown.output.strip() == version
    report = json.loads(runner.invoke(app, ["--home", str(tmp_path), "doctor"]).output)
    assert report["version"] == version
    from shorts_local.telegram import handle_update

    reply = handle_update(Store(tmp_path), update("/status"), 42)
    assert f"버전: {version}" in reply["text"]


def test_update_applies_only_a_newer_release_on_the_matching_clean_checkout(tmp_path, monkeypatch):
    import shorts_local.updates as updates

    repo = checkout(tmp_path / "repo")
    calls = []
    monkeypatch.setattr(updates, "latest_tag", lambda: "v0.2.0")
    monkeypatch.setattr(updates, "project_root", lambda: repo)
    monkeypatch.setattr(updates, "selected_extras", lambda: ["mlx"])
    monkeypatch.setattr(updates, "run_command", lambda command, cwd: calls.append((command, cwd)))
    result = CliRunner().invoke(app, ["--home", str(tmp_path / "home"), "update"])
    assert result.exit_code == 0, result.output
    assert calls == [
        (["git", "fetch", "origin", "tag", "v0.2.0", "--no-tags"], repo),
        (["git", "checkout", "--detach", "v0.2.0"], repo),
        (["uv", "sync", "--extra", "mlx"], repo),
    ]
    calls.clear()
    monkeypatch.setattr(updates, "latest_tag", lambda: "v0.1.0")
    current = CliRunner().invoke(app, ["--home", str(tmp_path / "home"), "update"])
    assert current.exit_code == 0, current.output
    assert calls == []
    assert current.output.strip() == f"{project_version()} 최신 릴리즈입니다."


def test_update_refuses_dirty_checkout_wrong_remote_and_running_service(tmp_path, monkeypatch):
    import shorts_local.updates as updates

    repo = checkout(tmp_path / "repo")
    (repo / "local.txt").write_text("changed\n")
    monkeypatch.setattr(updates, "latest_tag", lambda: "v0.2.0")
    monkeypatch.setattr(updates, "project_root", lambda: repo)
    monkeypatch.setattr(updates, "run_command", lambda *args: pytest.fail("update ran"))
    dirty = CliRunner().invoke(app, ["--home", str(tmp_path / "home"), "update"])
    assert dirty.exit_code != 0 and "로컬 수정" in dirty.output
    (repo / "local.txt").unlink()
    other = checkout(tmp_path / "other", "https://github.com/example/AutoShorts.git")
    monkeypatch.setattr(updates, "project_root", lambda: other)
    wrong = CliRunner().invoke(app, ["--home", str(tmp_path / "home"), "update"])
    assert wrong.exit_code != 0 and "origin" in wrong.output
    monkeypatch.setattr(updates, "project_root", lambda: repo)
    with exclusive_lock(tmp_path / "home" / "service.lock"):
        running = CliRunner().invoke(app, ["--home", str(tmp_path / "home"), "update"])
    assert running.exit_code != 0 and "실행 중" in running.output


def test_telegram_update_reports_a_release_without_changing_files(tmp_path, monkeypatch):
    import shorts_local.updates as updates
    from shorts_local.telegram import handle_update

    monkeypatch.setattr(updates, "latest_tag", lambda: "v0.2.0")
    monkeypatch.setattr(updates, "run_command", lambda *args: pytest.fail("update ran"))
    reply = handle_update(Store(tmp_path), update("/update", update_id=7), 42)
    assert "v0.2.0" in reply["text"] and "파일을 바꾸지 않습니다" in reply["text"]
    again = handle_update(Store(tmp_path), update("/update", update_id=7), 42)
    assert again == reply
