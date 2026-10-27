import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .models import VideoMetadata
from .process import BlockedError, QuotaError, atomic_json, exclusive_lock
from .profiles import UploadTarget, validate_channel_id, validate_youtube_id
from .store import Store

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TOKEN_WARN_AFTER = 6 * 86400


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class YouTubeChannel:
    id: str
    title: str


def auth_path(root: Path, channel_id: str) -> Path:
    return root / "channels" / validate_channel_id(channel_id) / "auth/youtube.json"


def read_auth(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
        if "youtube_channel_id" in data or "credentials" in data:
            validate_youtube_id(data.get("youtube_channel_id"))
            if not isinstance(data.get("credentials"), dict):
                raise ValueError
        return data
    except (OSError, ValueError):
        raise BlockedError("YouTube 인증 파일을 읽을 수 없습니다. 해당 채널을 다시 연결하세요.") from None


def identify_channel(token: Callable[[], str], client: httpx.Client | None = None) -> YouTubeChannel:
    with YouTubeUploader(token, client) as api:
        response = api.request("GET", "https://www.googleapis.com/youtube/v3/channels",
                               params={"part": "id,snippet", "mine": "true"})
    try:
        data = response.json()
        items = data["items"]
        if response.status_code != 200 or len(items) != 1 or data.get("nextPageToken"):
            raise ValueError
        identity = validate_youtube_id(items[0]["id"])
        title = items[0]["snippet"]["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError
        return YouTubeChannel(identity, title)
    except (ValueError, KeyError, TypeError):
        raise BlockedError("연결할 YouTube 채널을 하나로 확인할 수 없습니다. 채널을 선택해 다시 로그인하세요.") from None


def login(store: Store, channel_id: str, client_file: Path) -> YouTubeChannel:
    from google_auth_oauthlib.flow import InstalledAppFlow

    store.profile(channel_id)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    identity = identify_channel(lambda: credentials.token)
    path = auth_path(store.root, channel_id)
    with exclusive_lock(path.parent.parent / "auth.lock"), store.transaction():
        profile = store.profile(channel_id)
        if profile.youtube_channel_id not in (None, identity.id):
            raise BlockedError("프로필과 다른 YouTube 채널입니다. 별도 프로필로 연결하세요.")
        if path.exists():
            try:
                previous = read_auth(path)
            except BlockedError:
                if profile.youtube_channel_id != identity.id:
                    raise
                # A verified, immutable DB binding authorizes same-channel repair.
                # Preserve the damaged bytes before installing the fresh credentials.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix="youtube.damaged-",
                                                 suffix=".json", delete=False) as backup:
                    backup.write(path.read_bytes())
                    backup.flush()
                    os.fsync(backup.fileno())
            else:
                if previous.get("youtube_channel_id") not in (None, identity.id):
                    raise BlockedError("기존 인증 파일과 다른 YouTube 채널입니다. 별도 프로필로 연결하세요.")
        store.bind_channel(channel_id, identity.id, identity.title)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        atomic_json(path, {"youtube_channel_id": identity.id,
                           "credentials": json.loads(credentials.to_json()),
                           "granted_at": time.time()})
        store.update_profile_settings(channel_id, {"youtube_enabled": True})
    return identity


def warn_expiring_tokens(store: Store, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    warned = 0
    for profile in store.profiles():
        if not profile.youtube_channel_id or not profile.settings.youtube_enabled:
            continue
        try:
            granted = read_auth(auth_path(store.root, profile.id)).get("granted_at")
        except BlockedError:
            continue
        if not isinstance(granted, (int, float)) or isinstance(granted, bool):
            continue
        if now - granted < TOKEN_WARN_AFTER:
            continue
        date = datetime.fromtimestamp(now, ZoneInfo(profile.settings.timezone)).date().isoformat()
        key = f"youtube_token_warned:{profile.id}:{date}"
        if store.value(key, False):
            continue
        store.notify(f"{profile.label}\nYouTube 인증이 6일을 넘겼습니다. "
                     "OAuth 동의 화면을 In production으로 게시하거나 다시 로그인하세요.")
        store.set_value(key, True)
        warned += 1
    return warned


def _token_provider(path: Path, lock: Path, expected: str | None, *, legacy: bool = False):
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    def token():
        with exclusive_lock(lock):
            data = read_auth(path)
            if data.get("youtube_channel_id") not in (None, expected):
                raise BlockedError("인증 파일과 프로필의 YouTube 채널이 다릅니다.")
            info = data.get("credentials", data)
            try:
                credentials = Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
            except (ValueError, TypeError):
                raise BlockedError("YouTube 인증 정보를 해석할 수 없습니다. 다시 연결하세요.") from None
            if not credentials.valid:
                try:
                    credentials.refresh(Request())
                except TransportError:
                    raise RuntimeError("YouTube 인증 서버 연결 실패. 연결 회복 후 재시도합니다.") from None
                except RefreshError as exc:
                    if exc.retryable:
                        raise RuntimeError("YouTube 인증 서버 일시 오류. 잠시 후 재시도합니다.") from None
                    raise BlockedError("YouTube 인증 갱신 실패. 계정을 다시 연결하세요.") from None
                if not legacy:
                    updated = json.loads(credentials.to_json())
                    atomic_json(path, {**data, "credentials": updated} if "credentials" in data else updated)
            return credentials.token
    return token


def token_provider(root: Path, channel_id: str = "default") -> Callable[[], str]:
    profile = Store(root).profile(channel_id)
    path = auth_path(root, channel_id)
    return _token_provider(path, path.parent.parent / "auth.lock", profile.youtube_channel_id)


def legacy_token_provider(root: Path) -> Callable[[], str]:
    return _token_provider(root / "auth/youtube.json", root / "auth/legacy.lock", None, legacy=True)


def read_upload_artifact(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise BlockedError("업로드 복구 기록을 읽을 수 없습니다. Studio에서 결과를 확인하세요.") from None


def check_artifact_target(data: dict, target: UploadTarget | None, legacy: bool):
    present = {"channel_id", "youtube_channel_id"} & data.keys()
    if not present and legacy:
        return
    if target is None or any(data.get(k) != v for k, v in target.artifact_fields().items()):
        raise BlockedError("업로드 복구 기록의 대상 채널이 작업과 다릅니다.")


def completed_upload(video: Path, workdir: Path, *, target: UploadTarget | None,
                     legacy: bool = False) -> str | None:
    path = workdir / "upload-receipt.json"
    if not path.exists():
        return None
    receipt = read_upload_artifact(path)
    check_artifact_target(receipt, target, legacy)
    if receipt.get("upload_status") in {"rejected", "failed"}:
        raise BlockedError("YouTube가 업로드를 거부한 작업입니다. Studio에서 사유를 확인하세요.")
    if receipt.get("sha256") != file_hash(video):
        raise BlockedError("이미 업로드된 작업의 영상 파일이 변경되었습니다.")
    video_id = receipt.get("video_id")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise BlockedError("업로드 영수증에 올바른 영상 ID가 없습니다.")
    return video_id


class YouTubeUploader:
    def __init__(self, token, client: httpx.Client | None = None, chunk_size: int = 8 * 1024**2,
                 sleep=time.sleep):
        self.token = token
        self.client = client or httpx.Client(timeout=120, follow_redirects=False)
        self.owns_client = client is None
        self.chunk_size = chunk_size
        self.sleep = sleep

    def verify_upload(self, video_id: str) -> dict:
        result = {"upload_status": None, "privacy_status": None, "verified_at": time.time()}
        for attempt in range(3):
            if attempt:
                self.sleep(2)
            try:
                response = self.request("GET", VIDEOS_URL,
                                        params={"part": "status,processingDetails", "id": video_id})
                status = response.json()["items"][0]["status"]
                if not isinstance(status, dict):
                    continue
            except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
                continue
            result.update(upload_status=status.get("uploadStatus"),
                          privacy_status=status.get("privacyStatus"),
                          verified_at=time.time())
            if result["upload_status"] in {"uploaded", "processed", "rejected", "failed"}:
                break
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_client:
            self.client.close()

    def request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token()}"
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise RuntimeError("YouTube 연결이 끊겼습니다. 기존 업로드 세션에서 재개합니다.") from None
        if response.status_code == 429:
            raise QuotaError("YouTube 무료 할당량 회복을 기다립니다.")
        if response.status_code in (400, 401, 403):
            try:
                reasons = {x.get("reason") for x in response.json().get("error", {}).get("errors", [])}
            except ValueError:
                reasons = set()
            if reasons & {"quotaExceeded", "rateLimitExceeded", "uploadLimitExceeded"}:
                raise QuotaError("YouTube 업로드 한도에 도달했습니다.")
            raise BlockedError(f"YouTube 요청 또는 계정 설정 확인이 필요합니다 (HTTP {response.status_code}).")
        if response.status_code >= 500:
            raise RuntimeError(f"YouTube 서버 오류 (HTTP {response.status_code}).")
        return response

    @staticmethod
    def session_url(url: str) -> str:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "upload.googleapis.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise BlockedError("올바르지 않은 업로드 세션 주소입니다.")
        return url

    def upload(self, video: Path, metadata: VideoMetadata, settings: Settings, workdir: Path, *,
               target: UploadTarget, legacy: bool = False) -> str:
        existing = completed_upload(video, workdir, target=target, legacy=legacy)
        if existing:
            return existing
        if identify_channel(self.token, self.client).id != target.youtube_id:
            raise BlockedError("인증된 YouTube 채널과 작업의 업로드 대상이 다릅니다.")
        digest, total = file_hash(video), video.stat().st_size
        state_file, receipt_file = workdir / "upload-state.json", workdir / "upload-receipt.json"

        def completed(response):
            try:
                video_id = response.json()["id"]
            except (ValueError, KeyError):
                raise BlockedError("업로드 완료 여부가 불확실합니다. YouTube Studio에서 확인하세요.") from None
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                raise BlockedError("YouTube 응답에 올바른 영상 ID가 없습니다.")
            receipt = {"video_id": video_id, "sha256": digest, **target.artifact_fields()}
            atomic_json(receipt_file, receipt)
            verified = self.verify_upload(video_id)
            atomic_json(receipt_file, {**receipt, **verified})
            if verified["upload_status"] in {"rejected", "failed"}:
                raise BlockedError("YouTube가 업로드를 거부했습니다. Studio에서 사유를 확인하세요.")
            return video_id

        def offset_from(response):
            if response.status_code in (404, 410):
                raise BlockedError("세션이 만료되어 업로드 결과가 불확실합니다. Studio 확인 후 reconcile을 사용하세요.")
            if response.status_code != 308:
                raise BlockedError(f"업로드 응답 확인이 필요합니다 (HTTP {response.status_code}).")
            value = response.headers.get("Range")
            if value is None:
                return 0
            match = re.fullmatch(r"bytes=0-(\d+)", value)
            if not match:
                raise BlockedError("업로드 재개 위치를 해석할 수 없습니다.")
            offset = int(match[1]) + 1
            if offset > total:
                raise BlockedError("업로드 재개 위치가 파일 크기를 초과합니다.")
            return offset

        if state_file.exists():
            state = read_upload_artifact(state_file)
            check_artifact_target(state, target, legacy)
            if state.get("sha256") != digest or state.get("size") != total:
                raise BlockedError("진행 중인 업로드의 원본 파일이 변경되었습니다.")
            if not isinstance(state.get("url"), str):
                raise BlockedError("업로드 복구 기록에 세션 주소가 없습니다.")
            url = self.session_url(state["url"])
            if "channel_id" not in state:
                atomic_json(state_file, {**state, **target.artifact_fields()})
            response = self.request("PUT", url, content=b"", headers={"Content-Range": f"bytes */{total}"})
            if response.status_code in (200, 201):
                return completed(response)
            offset = offset_from(response)
        else:
            metadata = {
                "snippet": {"title": metadata.title, "description": metadata.description + "\n\n#Shorts",
                            "categoryId": settings.category_id, "defaultLanguage": "ko"},
                "status": {"privacyStatus": settings.privacy, "selfDeclaredMadeForKids": settings.made_for_kids,
                           "containsSyntheticMedia": settings.contains_synthetic_media},
            }
            response = self.request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                    json=metadata, headers={"X-Upload-Content-Type": "video/mp4",
                                                            "X-Upload-Content-Length": str(total)})
            if response.status_code not in (200, 201) or "Location" not in response.headers:
                raise BlockedError(f"업로드 세션을 시작하지 못했습니다 (HTTP {response.status_code}).")
            url = self.session_url(response.headers["Location"])
            atomic_json(state_file, {"url": url, "sha256": digest, "size": total, **target.artifact_fields()})
            offset = 0
        with video.open("rb") as stream:
            while offset < total:
                stream.seek(offset)
                chunk = stream.read(self.chunk_size)
                end = offset + len(chunk) - 1
                response = self.request("PUT", url, content=chunk, headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {offset}-{end}/{total}",
                })
                if response.status_code in (200, 201):
                    return completed(response)
                next_offset = offset_from(response)
                if next_offset <= offset:
                    raise RuntimeError("업로드가 진행되지 않았습니다. 같은 세션에서 다시 시도합니다.")
                offset = next_offset
        raise BlockedError("모든 바이트 전송 후 업로드 결과가 불확실합니다. Studio에서 확인하세요.")
