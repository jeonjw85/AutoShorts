import logging

import httpx
import pytest


@pytest.mark.parametrize("failed", [False, True])
def test_daemon_does_not_log_bot_tokens_or_upload_sessions(tmp_path, story_data, caplog, failed):
    from shorts_local.config import Settings
    from shorts_local.models import Story
    from shorts_local.service import configure_logging
    from shorts_local.telegram import Telegram
    from shorts_local.youtube import YouTubeUploader
    from tests.test_youtube import target, transport

    configure_logging()
    caplog.set_level(logging.INFO)
    secrets = ["123456:private-bot-token", "private-upload-session"]
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=" + secrets[1]

    def server(request):
        if request.url.host == "api.telegram.org":
            return httpx.Response(401 if failed else 200, json={"ok": not failed, "result": []})
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session})
        return httpx.Response(503 if failed else 200, json={"id": "safevideo01"})

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    with httpx.Client(transport=transport(server)) as client:
        actions = [lambda: Telegram(secrets[0], 42, client).poll(0),
                   lambda: YouTubeUploader(lambda: "access-token", client).upload(
                        video, Story(**story_data), Settings(), tmp_path, target=target())]
        for action in actions:
            if failed:
                with pytest.raises(RuntimeError):
                    action()
            else:
                action()
    assert all(secret not in caplog.text for secret in secrets)


@pytest.mark.parametrize("failure,blocked", [("transport", False), ("temporary", False), ("revoked", True)])
def test_oauth_refresh_only_blocks_unusable_credentials(tmp_path, monkeypatch, failure, blocked):
    from google.auth.exceptions import RefreshError, TransportError
    from google.oauth2.credentials import Credentials

    from shorts_local.process import BlockedError
    from shorts_local.youtube import token_provider

    secret = "sensitive-refresh-response"
    errors = {"transport": TransportError(secret),
              "temporary": RefreshError(secret, retryable=True),
              "revoked": RefreshError(secret)}

    class ExpiredCredentials:
        valid = False

        def refresh(self, request):
            raise errors[failure]

    from shorts_local.process import atomic_json
    from shorts_local.store import Store

    identity = "UC" + "A" * 22
    Store(tmp_path).bind_channel("default", identity, "test")
    atomic_json(tmp_path / "channels/default/auth/youtube.json", {
        "youtube_channel_id": identity, "credentials": {},
    })
    monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda *_, **__: ExpiredCredentials())
    with pytest.raises(RuntimeError) as caught:
        token_provider(tmp_path)()
    assert isinstance(caught.value, BlockedError) is blocked
    assert secret not in str(caught.value)
