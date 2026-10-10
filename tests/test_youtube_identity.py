import json
from types import SimpleNamespace

import httpx
import pytest

from shorts_local.process import BlockedError, atomic_json
from shorts_local.store import Store

A, B = "UC" + "A" * 22, "UC" + "B" * 22


def install_oauth(monkeypatch):
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = SimpleNamespace(token="candidate-token", to_json=lambda: json.dumps({
        "token": "candidate-token", "refresh_token": "fixture-refresh", "client_id": "fixture",
        "client_secret": "fixture-secret", "token_uri": "https://oauth2.googleapis.com/token",
    }))

    def flow(path, scopes):
        assert set(scopes) == {"https://www.googleapis.com/auth/youtube.upload",
                               "https://www.googleapis.com/auth/youtube.readonly"}
        return SimpleNamespace(run_local_server=lambda **_: credentials)

    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", flow)


@pytest.mark.parametrize("body", [
    {"items": []}, {"items": [{"id": A}, {"id": B}]},
    {"items": [{"id": A, "snippet": {"title": "A"}}], "nextPageToken": "more"},
    {"items": [{"id": "wrong", "snippet": {"title": "A"}}]}, ["not-an-object"],
])
def test_identity_requires_exactly_one_valid_channel(body):
    from shorts_local.youtube import identify_channel

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(BlockedError):
            identify_channel(lambda: "fixture", client)


def test_identity_reads_authenticated_channel():
    from shorts_local.youtube import identify_channel

    def server(request):
        assert request.url.path == "/youtube/v3/channels"
        assert request.url.params["mine"] == "true"
        assert request.url.params["part"] == "id,snippet"
        assert request.headers["authorization"] == "Bearer fixture"
        return httpx.Response(200, json={"items": [{"id": A, "snippet": {"title": "동물"}}]})

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        identity = identify_channel(lambda: "fixture", client)
        assert identity.id == A and identity.title == "동물"
        assert not client.is_closed


def test_login_only_enables_selected_profile(tmp_path, monkeypatch):
    import shorts_local.youtube as yt

    install_oauth(monkeypatch)
    s = Store(tmp_path)
    s.add_profile("animals", "동물")
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(A, "동물 채널"))
    yt.login(s, "animals", tmp_path / "client.json")
    assert s.profile("animals").youtube_channel_id == A
    assert s.profile("animals").settings.youtube_enabled
    assert not s.profile().settings.youtube_enabled
    path = tmp_path / "channels/animals/auth/youtube.json"
    assert json.loads(path.read_text())["youtube_channel_id"] == A
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "channels/default/auth/youtube.json").exists()


def test_competing_logins_keep_original_binding(tmp_path, monkeypatch):
    import shorts_local.youtube as yt

    install_oauth(monkeypatch)
    s = Store(tmp_path)
    path = tmp_path / "channels/default/auth/youtube.json"

    def competing(_):
        s.bind_channel("default", A, "먼저 연결")
        atomic_json(path, {"youtube_channel_id": A, "credentials": {"token": "original"}})
        return yt.YouTubeChannel(B, "늦은 다른 연결")

    monkeypatch.setattr(yt, "identify_channel", competing)
    with pytest.raises((ValueError, BlockedError)):
        yt.login(s, "default", tmp_path / "client.json")
    assert s.profile().youtube_channel_id == A
    assert json.loads(path.read_text())["credentials"]["token"] == "original"
    s.add_profile("other", "다른 채널")
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(A, "중복"))
    with pytest.raises((ValueError, BlockedError)):
        yt.login(s, "other", tmp_path / "client.json")
    assert not (tmp_path / "channels/other/auth/youtube.json").exists()


def test_incomplete_login_cannot_publish(tmp_path, monkeypatch):
    import shorts_local.youtube as yt

    install_oauth(monkeypatch)
    s = Store(tmp_path)
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(A, "A"))
    original = s.update_profile_settings

    def fail(*args):
        raise RuntimeError("write failed before commit")

    monkeypatch.setattr(s, "update_profile_settings", fail)
    with pytest.raises(RuntimeError):
        yt.login(s, "default", tmp_path / "client.json")
    assert s.profile().youtube_channel_id is None
    assert not s.profile().settings.youtube_enabled
    monkeypatch.setattr(s, "update_profile_settings", original)
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(B, "B"))
    with pytest.raises(BlockedError):
        yt.login(s, "default", tmp_path / "client.json")
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(A, "A"))
    yt.login(s, "default", tmp_path / "client.json")
    assert s.profile().youtube_channel_id == A


def test_refresh_preserves_channel_metadata_and_redacts_errors(tmp_path, monkeypatch):
    from google.oauth2.credentials import Credentials

    from shorts_local.youtube import token_provider

    s = Store(tmp_path)
    s.bind_channel("default", A, "A")
    path = tmp_path / "channels/default/auth/youtube.json"
    atomic_json(path, {"youtube_channel_id": A, "credentials": {"refresh_token": "old"}})

    class Expired:
        valid = False

        def __init__(self, info):
            self.info = info

        def refresh(self, request):
            self.token = self.info["refresh_token"] + "-fresh"
            self.info["token"] = self.token

        def to_json(self):
            return json.dumps(self.info)

    monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda info, **_: Expired(info))
    token = token_provider(tmp_path)
    atomic_json(path, {"youtube_channel_id": A, "credentials": {"refresh_token": "new"}})
    assert token() == "new-fresh"
    assert json.loads(path.read_text())["youtube_channel_id"] == A
    assert json.loads(path.read_text())["credentials"]["refresh_token"] == "new"


def test_same_identity_relogin_repairs_damaged_credentials(tmp_path, monkeypatch):
    import shorts_local.youtube as yt

    install_oauth(monkeypatch)
    s = Store(tmp_path)
    s.bind_channel("default", A, "원래 채널")
    path = tmp_path / "channels/default/auth/youtube.json"
    path.parent.mkdir(parents=True)
    damaged = b'{"token":"fixture-damaged",'
    path.write_bytes(damaged)
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(A, "다시 연결"))
    assert yt.login(s, "default", tmp_path / "client.json").id == A
    assert s.profile().youtube_channel_id == A and s.profile().settings.youtube_enabled
    assert json.loads(path.read_text())["youtube_channel_id"] == A
    backups = list(path.parent.glob("youtube.damaged-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == damaged
    assert backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("known, candidate", [(A, B), (None, A)])
def test_damaged_credentials_require_matching_established_identity(tmp_path, monkeypatch, known, candidate):
    import shorts_local.youtube as yt

    install_oauth(monkeypatch)
    s = Store(tmp_path)
    if known:
        s.bind_channel("default", known, "원래 채널")
    path = tmp_path / "channels/default/auth/youtube.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"damaged credential bytes")
    monkeypatch.setattr(yt, "identify_channel", lambda *_: yt.YouTubeChannel(candidate, "새 연결"))
    with pytest.raises((ValueError, BlockedError)):
        yt.login(s, "default", tmp_path / "client.json")
    assert path.read_bytes() == b"damaged credential bytes"
    assert s.profile().youtube_channel_id == known
    assert not list(path.parent.glob("youtube.damaged-*.json"))
