import json

import httpx
import pytest

from shorts_local.config import Settings
from shorts_local.models import Story

SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=private-session"
A, B = "UC" + "A" * 22, "UC" + "B" * 22


def target():
    from shorts_local.profiles import UploadTarget

    return UploadTarget(channel_id="default", youtube_id=A)


def transport(server, identity=A, verification=None):
    def route(request):
        if request.url.path == "/youtube/v3/channels":
            return httpx.Response(200, json={"items": [{"id": identity, "snippet": {"title": "test"}}]})
        if request.url.path == "/youtube/v3/videos":
            if verification:
                return verification(request)
            return httpx.Response(200, json={"items": [{"status": {"uploadStatus": "uploaded",
                                                                   "privacyStatus": "public"}}]})
        return server(request)
    return httpx.MockTransport(route)


def test_resumable_upload_and_receipt_prevent_a_second_upload(tmp_path, story_data):
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 600000)
    uploaded = bytearray()
    starts = []

    def server(request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["status"]["privacyStatus"] == "public"
            assert body["status"]["containsSyntheticMedia"] is True
            starts.append(request)
            return httpx.Response(200, headers={"Location": SESSION})
        expected_start = len(uploaded)
        assert request.headers["content-range"].startswith(f"bytes {expected_start}-")
        uploaded.extend(request.content)
        if len(uploaded) < 600000:
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(uploaded) - 1}"})
        return httpx.Response(200, json={"id": "video123456"})

    with httpx.Client(transport=transport(server)) as client:
        uploader = YouTubeUploader(lambda: "token", client, chunk_size=262144)
        for _ in range(2):
            assert uploader.upload(video, Story(**story_data), Settings(), tmp_path, target=target()) == "video123456"
    assert len(starts) == 1
    assert len(uploaded) == 600000
    assert (tmp_path / "upload-state.json").stat().st_mode & 0o777 == 0o600


def test_lost_final_response_is_recovered_without_a_new_session(tmp_path, story_data):
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def server(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        if request.headers["content-range"] == "bytes */5":
            return httpx.Response(200, json={"id": "recovered12"})
        raise httpx.ReadTimeout("secret URL must not leak", request=request)

    with httpx.Client(transport=transport(server)) as client:
        uploader = YouTubeUploader(lambda: "token", client)
        with pytest.raises(RuntimeError):
            uploader.upload(video, Story(**story_data), Settings(), tmp_path, target=target())
        assert uploader.upload(video, Story(**story_data), Settings(), tmp_path, target=target()) == "recovered12"
    assert calls == ["POST", "PUT", "PUT"]
    assert json.loads((tmp_path / "upload-receipt.json").read_text())["youtube_channel_id"] == A
    assert json.loads((tmp_path / "upload-state.json").read_text())["channel_id"] == "default"


def test_expired_session_is_blocked_instead_of_duplicated(tmp_path, story_data):
    import hashlib

    from shorts_local.process import BlockedError, atomic_json
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    atomic_json(tmp_path / "upload-state.json", {
        "url": SESSION, "sha256": hashlib.sha256(b"video").hexdigest(), "size": 5,
        "channel_id": "default", "youtube_channel_id": A,
    })

    def server(request):
        assert request.method == "PUT"
        return httpx.Response(404)

    with httpx.Client(transport=transport(server)) as client:
        with pytest.raises(BlockedError, match="불확실"):
            YouTubeUploader(lambda: "token", client).upload(
                video, Story(**story_data), Settings(), tmp_path, target=target())


def test_upload_session_cannot_redirect_credentials_to_another_host(tmp_path, story_data):
    from shorts_local.process import BlockedError
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    with httpx.Client(transport=transport(lambda _: httpx.Response(
        200, headers={"Location": "https://evil.test/upload"}))) as client:
        with pytest.raises(BlockedError):
            YouTubeUploader(lambda: "secret", client).upload(
                video, Story(**story_data), Settings(), tmp_path, target=target())


def test_channel_upload_limit_waits_even_when_youtube_returns_http_400(tmp_path, story_data):
    from shorts_local.process import QuotaError
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    response = httpx.Response(400, json={"error": {"errors": [{"reason": "uploadLimitExceeded"}]}})
    with httpx.Client(transport=transport(lambda _: response)) as client:
        with pytest.raises(QuotaError):
            YouTubeUploader(lambda: "secret", client).upload(
                video, Story(**story_data), Settings(), tmp_path, target=target())


@pytest.mark.parametrize("wrong", ["identity", "state", "receipt"])
def test_wrong_identity_or_artifact_target_never_transfers(tmp_path, story_data, wrong):
    import hashlib

    from shorts_local.process import BlockedError, atomic_json
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    if wrong != "identity":
        atomic_json(tmp_path / f"upload-{wrong}.json", {
            "sha256": hashlib.sha256(b"video").hexdigest(), "size": 5, "url": SESSION,
            "channel_id": "default", "youtube_channel_id": B, "video_id": "video123456",
        })
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=transport(server, B if wrong == "identity" else A)) as client:
        with pytest.raises(BlockedError):
            YouTubeUploader(lambda: "fixture", client).upload(
                video, Story(**story_data), Settings(), tmp_path, target=target())
    assert not calls


def test_missing_target_is_only_accepted_for_legacy_receipts(tmp_path):
    import hashlib

    from shorts_local.process import BlockedError, atomic_json
    from shorts_local.youtube import completed_upload

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    receipt = tmp_path / "upload-receipt.json"
    data = {"video_id": "video123456", "sha256": hashlib.sha256(b"video").hexdigest()}
    atomic_json(receipt, data)
    with pytest.raises(BlockedError):
        completed_upload(video, tmp_path, target=target())
    assert completed_upload(video, tmp_path, target=None, legacy=True) == "video123456"
    video.write_bytes(b"changed")
    with pytest.raises(BlockedError):
        completed_upload(video, tmp_path, target=None, legacy=True)
    video.write_bytes(b"video")
    atomic_json(receipt, {**data, "channel_id": "default"})
    with pytest.raises(BlockedError):
        completed_upload(video, tmp_path, target=target(), legacy=True)
    receipt.write_text("broken json")
    with pytest.raises(BlockedError):
        completed_upload(video, tmp_path, target=None, legacy=True)


def test_legacy_session_is_enriched_before_resuming(tmp_path, story_data):
    import hashlib

    from shorts_local.process import BlockedError, atomic_json
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    state = tmp_path / "upload-state.json"
    atomic_json(state, {"url": SESSION, "sha256": hashlib.sha256(b"video").hexdigest(), "size": 5})
    transfers = []

    def server(request):
        transfers.append(request.method)
        assert request.method == "PUT" and str(request.url) == SESSION
        assert json.loads(state.read_text())["youtube_channel_id"] == A
        return httpx.Response(200, json={"id": "video123456"})

    with httpx.Client(transport=transport(server)) as client:
        uploader = YouTubeUploader(lambda: "fixture", client)
        with pytest.raises(BlockedError):
            uploader.upload(video, Story(**story_data), Settings(), tmp_path, target=target())
        assert transfers == []
        assert uploader.upload(video, Story(**story_data), Settings(), tmp_path,
                               target=target(), legacy=True) == "video123456"
    assert transfers == ["PUT"]


def test_verify_upload_polls_three_times_and_records_status():
    from shorts_local.youtube import YouTubeUploader

    calls, sleeps = [], []

    def server(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [{"status": {"uploadStatus": "uploaded",
                                                               "privacyStatus": "unlisted"}}]})

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        result = YouTubeUploader(lambda: "t", client, sleep=sleeps.append).verify_upload("video123456")
    assert len(calls) == 3 and sleeps == [2, 2]
    assert calls[0].url.params["part"] == "status,processingDetails"
    assert calls[0].url.params["id"] == "video123456"
    assert result["upload_status"] == "uploaded" and result["privacy_status"] == "unlisted"
    assert result["verified_at"] > 0


def test_verify_upload_survives_http_errors_without_blocking_the_upload():
    from shorts_local.youtube import YouTubeUploader

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        result = YouTubeUploader(lambda: "t", client, sleep=lambda _: None).verify_upload("video123456")
    assert result["upload_status"] is None and result["verified_at"] > 0


def test_rejected_upload_status_blocks_every_retry(tmp_path, story_data):
    from shorts_local.process import BlockedError
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 1000)

    def server(request):
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(200, json={"id": "video123456"})

    def verification(_):
        return httpx.Response(200, json={"items": [{"status": {"uploadStatus": "rejected"}}]})

    with httpx.Client(transport=transport(server, verification=verification)) as client:
        uploader = YouTubeUploader(lambda: "t", client, sleep=lambda _: None)
        with pytest.raises(BlockedError):
            uploader.upload(video, Story(**story_data), Settings(), tmp_path, target=target())
    receipt = json.loads((tmp_path / "upload-receipt.json").read_text())
    assert receipt["upload_status"] == "rejected" and receipt["video_id"] == "video123456"
    with httpx.Client(transport=transport(server, verification=verification)) as client:
        with pytest.raises(BlockedError):
            YouTubeUploader(lambda: "t", client, sleep=lambda _: None).upload(
                video, Story(**story_data), Settings(), tmp_path, target=target())


def test_upload_receipt_records_verification_fields(tmp_path, story_data):
    from shorts_local.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 1000)

    def server(request):
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(200, json={"id": "video123456"})

    with httpx.Client(transport=transport(server)) as client:
        YouTubeUploader(lambda: "t", client).upload(
            video, Story(**story_data), Settings(), tmp_path, target=target())
    receipt = json.loads((tmp_path / "upload-receipt.json").read_text())
    assert receipt["upload_status"] == "uploaded"
    assert receipt["privacy_status"] == "public"
    assert receipt["verified_at"] > 0
