"""Bounded Instagram I/O. Optional SDK imports and credentials stay at this boundary."""

import argparse
import importlib.util
import json
import os
import random
import shutil
import sys
import time
import uuid
from datetime import UTC
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .instagram_models import (
    InstagramInput,
    InstagramPost,
    ScanBatch,
    SourceMedia,
    normalize_username,
)
from .media import probe
from .process import BlockedError, BusyError, atomic_json, exclusive_lock, run_process
from .store import Store
from .youtube import file_hash

MAX_SOURCE_BYTES = 300 * 1024**2
FULL_RESCAN_SECONDS = 86400
OLD_POST_STREAK = 20
HEAD_PROBE_ITEMS = 20
MESSAGES = {
    "busy": "다른 인스타그램 요청이 실행 중입니다.",
    "deferred": "인스타그램 요청 간격을 기다립니다.",
    "rate_limit": "인스타그램 요청 제한입니다. 대기 후 자동 재개합니다.",
    "auth_wait": "인스타그램 로그인이 필요합니다. 로컬 instagram-login으로 다시 연결하세요.",
    "unavailable": "계정 또는 원본이 삭제되었거나 접근할 수 없습니다.",
    "unsupported": "1초~10분, 300MiB 이하의 단일 동영상만 지원합니다.",
    "transient": "인스타그램 통신 또는 원본 검증에 실패했습니다. 자동 재시도합니다.",
}


class InstagramError(RuntimeError):
    def __init__(self, code: str, *, retry_at: float | None = None,
                 auth_generation: str | None = None):
        self.code = code if code in MESSAGES else "transient"
        self.retry_at = retry_at
        self.auth_generation = auth_generation
        super().__init__(MESSAGES[self.code])


def classify_error(error: Exception) -> InstagramError:
    if isinstance(error, InstagramError):
        return error
    if isinstance(error, BusyError):
        return InstagramError("busy", retry_at=time.time() + 5)
    import instaloader as sdk

    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, InstagramError):
            return error
        if isinstance(error, sdk.TooManyRequestsException):
            return InstagramError("rate_limit", retry_at=time.time() + 3600 * random.uniform(0.8, 1.2))
        if isinstance(error, (sdk.LoginRequiredException, sdk.LoginException)):
            return InstagramError("auth_wait")
        if isinstance(error, (sdk.ProfileNotExistsException, sdk.QueryReturnedNotFoundException,
                              sdk.PrivateProfileNotFollowedException, sdk.QueryReturnedForbiddenException)):
            return InstagramError("unavailable")
        if isinstance(error, (sdk.InstaloaderException, sdk.AbortDownloadException)):
            message = str(error).lower()
            if "feedback_required" in message:
                return InstagramError("rate_limit", retry_at=time.time() + 3600 * random.uniform(0.8, 1.2))
            if any(marker in message for marker in
                   ("checkpoint", "challenge_required", "login_required", "login page", "logged out")):
                return InstagramError("auth_wait")
        # max_connection_attempts=1 wraps HTTP errors in ConnectionException.
        error = error.__cause__ or error.__context__
    return InstagramError("transient")


def auth_wait_channels(store: Store) -> set[str]:
    if store.value("instagram_auth_wait", False):
        return {profile.id for profile in store.profiles()}
    values = store.value("instagram_auth_wait_channels", [])
    return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()


def check_wait(store: Store, channel_id: str | None = None, *, auth: bool = True) -> None:
    if auth:
        waiting = auth_wait_channels(store)
        if (channel_id is None and waiting) or (channel_id is not None and channel_id in waiting):
            raise InstagramError("auth_wait")
    until = store.value("instagram_next_run", 0)
    if until > time.time():
        raise InstagramError("deferred", retry_at=until)


def session_generation(root: Path) -> str:
    try:
        data = json.loads((root / "auth/instagram.json").read_text())
        generation = data["generation"]
        if (isinstance(generation, str) and len(generation) == 32
                and all(c in "0123456789abcdef" for c in generation)
                and isinstance(data.get("session"), dict) and data["session"]
                and normalize_username(data["username"])):
            return generation
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ""


def _loader(root: Path, *, load_session: bool = True):
    import instaloader as sdk

    store = Store(root)

    class PersistentRate(sdk.RateController):
        def __init__(self, context):
            super().__init__(context)
            offset = time.time() - time.monotonic()
            self._query_timestamps = {
                name: [t - offset for t in times if t > time.time() - 3600]
                for name, times in store.value("instagram_rate", {}).items()
            }

        def sleep(self, secs):
            raise InstagramError("deferred", retry_at=time.time() + secs)

        def wait_before_query(self, query_type):
            super().wait_before_query(query_type)
            offset = time.time() - time.monotonic()
            store.set_value("instagram_rate", {
                key: [t + offset for t in values] for key, values in self._query_timestamps.items()
            })

        def handle_429(self, query_type):
            wait = 3600
            if self._query_timestamps.get(query_type):
                wait = max(wait, self.query_waittime(query_type, time.monotonic(), True))
            raise InstagramError("rate_limit", retry_at=time.time() + wait * random.uniform(0.8, 1.2))

    loader = sdk.Instaloader(quiet=True, sleep=False, max_connection_attempts=1,
                            request_timeout=20, rate_controller=PersistentRate)
    loader.context.error = lambda *args, **kwargs: None
    loader.context.log = lambda *args, **kwargs: None
    path = root / "auth/instagram.json"
    if load_session and path.exists():
        try:
            data = json.loads(path.read_text())
            username = normalize_username(data["username"])
            if not isinstance(data["session"], dict):
                raise ValueError
            loader.load_session(username, data["session"])
        except (ValueError, KeyError, TypeError):
            loader.close()
            raise InstagramError("auth_wait") from None
    return loader


def load_session_file(path: Path, *, username: str | None = None) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError("세션 파일을 JSON으로 읽을 수 없습니다.") from None
    if not isinstance(data, dict):
        raise ValueError("세션 파일은 JSON 객체여야 합니다.")
    if "session" in data:
        if username and data.get("username") not in (None, username):
            raise ValueError("세션 파일의 사용자 이름이 다릅니다.")
        data = data["session"]
    if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and data[key]
            for key in ("sessionid", "csrftoken", "ds_user_id")):
        raise ValueError("세션 파일에 sessionid, csrftoken, ds_user_id가 필요합니다.")
    return data


def login_instagram(store: Store, username: str, *, prompt_secret=None,
                    session_file: Path | None = None) -> str:
    import instaloader as sdk

    username = normalize_username(username)
    try:
        with exclusive_lock(store.root / "instagram.lock"):
            check_wait(store, auth=False)
            loader = _loader(store.root, load_session=False)
            try:
                if session_file is not None:
                    loader.load_session(username, load_session_file(session_file, username=username))
                    if loader.test_login() != username:
                        raise ValueError(f"세션 파일이 @{username} 계정과 맞지 않습니다.")
                else:
                    if prompt_secret is None:
                        raise ValueError("비밀번호 입력이 필요합니다.")
                    try:
                        loader.login(username, prompt_secret("인스타그램 비밀번호"))
                    except sdk.TwoFactorAuthRequiredException:
                        loader.two_factor_login(prompt_secret("인스타그램 2단계 인증 코드"))
                    if loader.test_login() != username:
                        raise InstagramError("auth_wait")
                generation = uuid.uuid4().hex
                atomic_json(store.root / "auth/instagram.json", {
                    "username": username, "session": loader.save_session(), "generation": generation,
                })
                from .instagram_store import InstagramStore

                InstagramStore(store).reconcile_auth()
                return username
            finally:
                loader.close()
    except ValueError:
        raise
    except Exception as exc:
        raise classify_error(exc) from None


def _post_value(post) -> InstagramPost:
    kind = {"GraphVideo": "video", "GraphImage": "photo", "GraphSidecar": "carousel"}.get(
        post.typename, "unsupported")
    when = post.date_utc
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    return InstagramPost(shortcode=post.shortcode,
                         url=f"https://www.instagram.com/reel/{post.shortcode}/",
                         author=post.owner_username, owner_id=str(post.owner_id),
                         published_at=when.timestamp(), caption=post.caption or "", media_type=kind)


def scan_source(root: Path, source: dict, workdir: Path) -> None:
    import instaloader as sdk
    from instaloader.nodeiterator import FrozenNodeIterator

    request = json.loads((workdir / "request.json").read_text())
    cursor = source["cursor"] or {}
    # Keep completion time separately from the full walk's start watermark, so
    # a long-running walk does not immediately trigger another full rescan.
    full_scan_at = cursor.get("full_scan_at", source.get("completed_through"))
    full_due = full_scan_at is None or time.time() - full_scan_at >= FULL_RESCAN_SECONDS
    resuming = "feeds" in cursor and "started_at" in cursor
    if not resuming or (cursor.get("mode") == "recent" and full_due):
        cursor = {"feeds": {}, "done": [], "next": 0, "started_at": time.time(),
                  "mode": "full" if full_due else "recent", "full_scan_at": full_scan_at,
                  "old_streak": {}}
        resuming = False
    else:
        cursor.setdefault("mode", "full")  # Legacy cursors always finish their full walk.
        cursor.setdefault("full_scan_at", full_scan_at)
        cursor.setdefault("old_streak", {})
    cutoff = max(source["registered_at"], source.get("completed_through") or 0)
    result = ScanBatch(run_id=request["run_id"], source_id=source["id"],
                       base_revision=source["scan_revision"], started_at=cursor["started_at"],
                       cursor=cursor)
    feeds, posts = {}, {}
    loader = None

    def checkpoint():
        for name, iterator in feeds.items():
            if name not in cursor["done"]:
                cursor["feeds"][name] = iterator.freeze()._asdict()
        result.posts = list(posts.values())
        result.complete = len(cursor["done"]) == 2 and result.error_code is None
        result.cursor = cursor
        if result.complete:
            result.cursor = {"mode": cursor["mode"], "full_scan_at": (
                time.time() if cursor["mode"] == "full" else cursor["full_scan_at"]
            )}
        atomic_json(workdir / "result.json", result.model_dump())

    try:
        deadline = time.monotonic() + 110
        loader = _loader(root)
        profile = (sdk.Profile.from_id(loader.context, int(source["instagram_id"]))
                   if source["instagram_id"] else sdk.Profile.from_username(loader.context, source["username"]))
        result.account_id, result.username = str(profile.userid), profile.username
        consumed = 0
        if resuming:
            # Frozen tails do not contain newly published heads. Keep these probes
            # separate so a wait/interruption cannot replace durable tail progress.
            checkpoint()
            for name in ("posts", "reels"):
                if time.monotonic() >= deadline:
                    break
                head = getattr(profile, f"get_{name}")()
                for _ in range(HEAD_PROBE_ITEMS):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        post = _post_value(next(head))
                    except StopIteration:
                        break
                    consumed += 1
                    if post.published_at >= source["registered_at"]:
                        posts[post.shortcode] = post
                    checkpoint()
        for name in ("posts", "reels"):
            if name in cursor["done"]:
                continue
            if time.monotonic() >= deadline:
                checkpoint()
                return
            iterator = getattr(profile, f"get_{name}")()
            frozen = cursor["feeds"].get(name)
            # Thaw replays the last node. Reset conservatively rather than count
            # that old node twice and stop a resumed recent scan too early.
            cursor["old_streak"][name] = 0
            if frozen and (frozen.get("best_before") or 0) > time.time():
                try:
                    iterator.thaw(FrozenNodeIterator(**frozen))
                except (sdk.InvalidArgumentException, ValueError, TypeError):
                    pass  # Restart this feed at its head; shortcode uniqueness makes replay safe.
            feeds[name] = iterator
        checkpoint()
        while len(cursor["done"]) < 2 and consumed < 200 and time.monotonic() < deadline:
            name = ("posts", "reels")[cursor["next"]]
            cursor["next"] = 1 - cursor["next"]
            if name in cursor["done"]:
                continue
            try:
                post = _post_value(next(feeds[name]))
            except StopIteration:
                cursor["done"].append(name)
            else:
                consumed += 1
                if post.published_at >= source["registered_at"]:
                    posts[post.shortcode] = post
                if cursor["mode"] == "recent":
                    # One old/pinned post cannot hide later new posts. Arbitrarily
                    # out-of-order or delayed posts are recovered by the daily full scan.
                    cursor["old_streak"][name] = (cursor["old_streak"][name] + 1
                                                   if post.published_at < cutoff else 0)
                    if cursor["old_streak"][name] >= OLD_POST_STREAK:
                        cursor["done"].append(name)
            checkpoint()
    except Exception as exc:
        error = classify_error(exc)
        result.error_code, result.retry_at = error.code, error.retry_at
        checkpoint()
    finally:
        if loader:
            loader.close()


def _media_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.port not in (None, 443)
                or not any(host == domain or host.endswith("." + domain)
                           for domain in ("cdninstagram.com", "fbcdn.net"))):
            raise ValueError
    except ValueError:
        raise InstagramError("unavailable") from None
    return value


def _download_media(url: str, target: Path, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=min(20, timeout), follow_redirects=False) as client:
            for _ in range(6):
                _media_url(url)
                client.cookies.clear()
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code in (401, 403, 404):
                        raise InstagramError("unavailable")
                    if response.status_code == 429:
                        raise InstagramError("rate_limit", retry_at=time.time() + 3600)
                    if response.status_code != 200:
                        raise InstagramError("transient")
                    if int(response.headers.get("content-length", 0)) > MAX_SOURCE_BYTES:
                        raise InstagramError("unsupported")
                    size = 0
                    with target.open("wb") as stream:
                        target.chmod(0o600)
                        for chunk in response.iter_bytes(64 * 1024):
                            if time.monotonic() > deadline:
                                raise InstagramError("transient")
                            size += len(chunk)
                            if size > MAX_SOURCE_BYTES:
                                raise InstagramError("unsupported")
                            stream.write(chunk)
                    if size == 0:
                        raise InstagramError("transient")
                    return
            raise InstagramError("unavailable")
    except Exception:
        target.unlink(missing_ok=True)
        raise


def cached_source(workdir: Path, input: InstagramInput) -> SourceMedia | None:
    try:
        media = SourceMedia.model_validate_json((workdir / "source.json").read_text())
        path = workdir / "source.mp4"
        if (media.post.shortcode == input.shortcode and path.stat().st_size == media.bytes
                and file_hash(path) == media.sha256):
            return media
    except (ValueError, OSError):
        pass
    return None


def _fetch_with(loader, input: InstagramInput, temp: Path, settings: Settings) -> SourceMedia:
    import instaloader as sdk

    post = sdk.Post.from_shortcode(loader.context, input.shortcode)
    metadata = _post_value(post)
    if metadata.media_type != "video" or not 1 <= float(post.video_duration) <= 600:
        raise InstagramError("unsupported")
    # In pinned Instaloader, video_url ranks variants via unchecked HEAD
    # redirects. Use the declared metadata URL; all media I/O belongs to
    # our validated, bounded downloader.
    _download_media(post._field("video_url"), temp, min(settings.stage_timeout, 600))
    info = probe(temp)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    duration = float(info["format"]["duration"])
    if len(videos) != 1 or not 1 <= duration <= 600:
        raise InstagramError("unsupported")
    return SourceMedia(post=metadata, sha256=file_hash(temp), bytes=temp.stat().st_size,
                       duration=duration, width=videos[0]["width"],
                       height=videos[0]["height"],
                       has_audio=any(s["codec_type"] == "audio" for s in info["streams"]))


def _fetch_media(root: Path, input: InstagramInput, workdir: Path, settings: Settings) -> SourceMedia:
    loader = _loader(root)
    temp = workdir / "source.partial"
    try:
        media = _fetch_with(loader, input, temp, settings)
        os.replace(temp, workdir / "source.mp4")
        atomic_json(workdir / "source.json", media.model_dump())
        return media
    except Exception as exc:
        temp.unlink(missing_ok=True)
        raise classify_error(exc) from None
    finally:
        loader.close()


def fetch_source(root: Path, input: InstagramInput, workdir: Path, settings: Settings,
                 channel_id: str | None = None) -> SourceMedia:
    if cached := cached_source(workdir, input):
        return cached
    if importlib.util.find_spec("instaloader") is None:
        raise BlockedError("수집 엔진 설치가 필요합니다: uv sync --extra mlx --extra images --extra instagram")
    workdir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(workdir).free < 5 * 1024**3:
        raise BlockedError("디스크 여유 공간이 5GiB 미만입니다. 공간 확보 후 재시도하세요.")
    store = Store(root)
    generation = None
    try:
        with exclusive_lock(root / "instagram.lock"):
            generation = session_generation(root)
            check_wait(store, channel_id)
            atomic_json(workdir / "input.json", input.model_dump())
            settings.save(workdir / "settings.json")
            result = workdir / "fetch-result.json"
            result.unlink(missing_ok=True)
            run_process([sys.executable, "-m", "shorts_local.instagram", "fetch", str(root), str(workdir)],
                        workdir / "fetch.log", min(settings.stage_timeout, 600))
            data = json.loads(result.read_text())
            if data.get("error_code"):
                raise InstagramError(data["error_code"], retry_at=data.get("retry_at"))
            if cached := cached_source(workdir, input):
                return cached
            raise InstagramError("transient")
    except Exception as exc:
        error = classify_error(exc)
        error.auth_generation = generation
        raise error from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["fetch", "scan"])
    parser.add_argument("root", type=Path)
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    try:
        if args.stage == "scan":
            request = json.loads((args.workdir / "request.json").read_text())
            scan_source(args.root, request["source"], args.workdir)
        else:
            input = InstagramInput.model_validate_json((args.workdir / "input.json").read_text())
            _fetch_media(args.root, input, args.workdir, Settings.load(args.workdir / "settings.json"))
            atomic_json(args.workdir / "fetch-result.json", {"ok": True})
    except Exception as exc:
        error = classify_error(exc)
        if args.stage == "fetch":
            atomic_json(args.workdir / "fetch-result.json", {
                "error_code": error.code, "retry_at": error.retry_at,
            })
        else:
            print(error, flush=True)


if __name__ == "__main__":
    main()
