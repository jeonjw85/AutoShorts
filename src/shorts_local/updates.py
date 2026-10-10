import fcntl
import re
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx

REPO = "jeonjw85/AutoShorts"
_RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"


class UpdateBlocked(Exception):
    """The checkout cannot be moved to a release."""


def installed_version() -> str:
    try:
        return version("shorts-local")
    except PackageNotFoundError as exc:
        raise UpdateBlocked("설치된 패키지 버전을 찾지 못했습니다.") from exc


def version_tuple(value: str) -> tuple[int, ...]:
    text = value.removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+)+", text):
        raise UpdateBlocked(f"버전을 읽을 수 없습니다: {value}")
    return tuple(int(part) for part in text.split("."))


def release_status(current: str, tag: str) -> str:
    if version_tuple(tag) <= version_tuple(current):
        return f"{current} 최신 릴리즈입니다."
    return f"{current} 사용 중. 새 릴리즈 {tag}가 있습니다."


def latest_tag() -> str:
    try:
        response = httpx.get(_RELEASES, timeout=20, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "shorts-local"})
        response.raise_for_status()
        tag = response.json()["tag_name"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise UpdateBlocked("GitHub 릴리즈를 확인하지 못했습니다.") from exc
    version_tuple(tag)
    return tag


def normalize_remote(url: str) -> str:
    text = url.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "ssh://git@github.com/", "git@github.com:"):
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def project_root(start: Path | None = None) -> Path:
    origin = (start or Path(__file__)).resolve()
    for candidate in (origin, *origin.parents):
        manifest = candidate / "pyproject.toml"
        if (manifest.is_file() and (candidate / ".git").exists()
                and 'name = "shorts-local"' in manifest.read_text()):
            return candidate
    raise UpdateBlocked("Git checkout에서만 업데이트할 수 있습니다.")


def selected_extras() -> list[str]:
    import importlib.util

    groups = {"mlx": ("mlx_lm", "mlx_audio"), "images": ("mflux",),
              "instagram": ("instaloader", "mlx_whisper", "mlx_vlm")}
    return [name for name, modules in groups.items()
            if any(importlib.util.find_spec(module) for module in modules)]


def _git_output(repo: Path, command: list[str]) -> str:
    result = subprocess.run(command, cwd=repo, check=False, capture_output=True, text=True)
    if result.returncode:
        raise UpdateBlocked(f"{command[0]} 상태를 확인하지 못했습니다.")
    return result.stdout


def run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit {result.returncode}"
        raise UpdateBlocked(f"{command[0]} 실패: {reason}")


def apply_release(repo: Path, home: Path, tag: str) -> None:
    if normalize_remote(_git_output(repo, ["git", "remote", "get-url", "origin"])) != REPO:
        raise UpdateBlocked("이 checkout의 origin이 jeonjw85/AutoShorts가 아닙니다.")
    if _git_output(repo, ["git", "status", "--porcelain"]).strip():
        raise UpdateBlocked("로컬 수정이 있어 업데이트하지 않습니다.")
    version_tuple(tag)
    streams = []
    try:
        for name in ("service.lock", "worker.lock"):
            path = home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a+")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise UpdateBlocked("실행 중인 서비스나 작업이 있어 업데이트하지 않습니다.") from None
            streams.append(stream)
        extras = [item for name in selected_extras() for item in ("--extra", name)]
        run_command(["git", "fetch", "origin", "tag", tag, "--no-tags"], repo)
        run_command(["git", "checkout", "--detach", tag], repo)
        run_command(["uv", "sync", *extras], repo)
    finally:
        for stream in streams:
            stream.close()
