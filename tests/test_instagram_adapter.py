import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from shorts_local.config import Settings
from shorts_local.instagram_models import InstagramInput, ScanBatch
from shorts_local.instagram_store import InstagramStore
from shorts_local.process import atomic_json
from shorts_local.store import Store
from tests.instagram_helpers import batch, post


def sdk_post(code, published=200, kind="GraphVideo"):
    return SimpleNamespace(shortcode=code, owner_username="pets", owner_id=123,
                           date_utc=datetime.fromtimestamp(published, UTC).replace(tzinfo=None),
                           caption="원본", typename=kind, is_video=kind == "GraphVideo",
                           video_duration=2, video_url="https://video.cdninstagram.com/file?secret=cap",
                           _field=lambda key: "https://video.cdninstagram.com/file?secret=cap")


def install_scan(monkeypatch, target, values, *, pinned=None, reels=None, seen=None):
    import instaloader

    context = SimpleNamespace(username="login")

    # Frozen data must be JSON, as in the real SDK. Wrap raw dictionaries into posts lazily.
    def feed(name, items):
        def wrap(node):
            if seen is not None:
                seen.append((name, node["code"]))
            return sdk_post(node["code"], node["published"])

        return instaloader.NodeIterator(context, "fixture", lambda d: d,
            wrap,
            first_data={"edges": [{"node": {"code": p.shortcode,
                                             "published": p.date_utc.replace(tzinfo=UTC).timestamp()}}
                                  for p in items], "page_info": {"has_next_page": False}})

    profile = SimpleNamespace(userid=123, username="renamed", get_posts=lambda: feed("posts", values),
                              get_reels=lambda: feed("reels", values[:1] if reels is None else reels))

    def from_username(ctx, name):
        if pinned:
            raise AssertionError("Pinned sources must not be resolved through their old name")
        return profile

    def from_id(ctx, id):
        assert id == 123
        return profile

    monkeypatch.setattr(instaloader.Profile, "from_username", from_username)
    monkeypatch.setattr(instaloader.Profile, "from_id", from_id)
    monkeypatch.setattr(target, "_loader", lambda root: SimpleNamespace(context=context, close=lambda: None))


def test_scan_does_not_stop_at_old_or_pinned_post(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    s = Store(tmp_path)
    source = InstagramStore(s).add_source("pets", now=100)
    values = [sdk_post("Old", 10), sdk_post("A", 200), sdk_post("Older", 20), sdk_post("B", 300)]
    install_scan(monkeypatch, target, values)
    folder = tmp_path / "scan"
    atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    result = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert {p.shortcode for p in result.posts} == {"A", "B"}
    assert result.complete and result.error_code is None
    values = [sdk_post(f"V{i}", 200) for i in range(205)]
    install_scan(monkeypatch, target, values)
    target.scan_source(tmp_path, source, folder)
    first = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert not first.complete and len(first.posts) <= 200
    source["cursor"] = first.cursor
    target.scan_source(tmp_path, source, folder)
    second = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert second.complete
    assert {p.shortcode for p in [*first.posts, *second.posts]} == {f"V{i}" for i in range(205)}


def test_recent_scan_skips_old_history_but_daily_rescan_finds_delayed_posts(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    ig = InstagramStore(Store(tmp_path))
    source = ig.add_source("pets", now=100)
    clock = [1000]
    monkeypatch.setattr(target.time, "time", lambda: clock[0])
    values = [sdk_post(f"Old{i}", 10) for i in range(205)]
    seen = []
    install_scan(monkeypatch, target, values, seen=seen)
    folder = tmp_path / "scan"

    def scan():
        source = ig.source(source_id)
        atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
        target.scan_source(tmp_path, source, folder)
        result = ScanBatch.model_validate_json((folder / "result.json").read_text())
        ig.apply_scan(result, now=clock[0])
        return result

    source_id = source["id"]
    assert not scan().complete
    clock[0] = 1300
    assert scan().complete
    assert ig.source(source_id)["completed_through"] == 1000
    values.insert(100, sdk_post("Delayed", 200))
    seen.clear()
    clock[0] = 1600
    recent = scan()
    assert recent.complete and len(seen) < 100
    assert not recent.posts
    after = ig.source(source_id)
    assert after["completed_through"] == 1000
    assert after["last_success"] == 1600
    assert after["next_run"] == 1600 + ig.store.profile().settings.instagram_poll_minutes * 60
    seen.clear()
    clock[0] = 87401  # A day since the walk started, but not since it finished.
    assert scan().complete and len(seen) < 100
    seen.clear()
    clock[0] = 87701
    full = scan()
    assert not full.complete and len(seen) == 200
    assert "Delayed" in {p.shortcode for p in full.posts}
    assert ig.source(source_id)["completed_through"] == 1000


def test_recent_scan_old_streaks_are_separate_and_tolerate_pinned_posts(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    ig = InstagramStore(Store(tmp_path))
    source = ig.add_source("pets", now=100)
    ig.apply_scan(batch(source, started_at=1000), now=1000)
    source = ig.source(source["id"])
    monkeypatch.setattr(target.time, "time", lambda: 1100)
    values = [sdk_post(f"PinnedP{i}", 10) for i in range(19)] + [sdk_post("NewPost", 1050)]
    reels = [sdk_post(f"PinnedR{i}", 10) for i in range(19)] + [sdk_post("NewReel", 1050)]
    values.extend(sdk_post(f"OldP{i}", 10) for i in range(205))
    reels.extend(sdk_post(f"OldR{i}", 10) for i in range(205))
    seen = []
    install_scan(monkeypatch, target, values, reels=reels, seen=seen)
    folder = tmp_path / "scan"
    atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    result = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert result.complete and len(seen) < 100
    assert {p.shortcode for p in result.posts} == {"NewPost", "NewReel"}
    ig.apply_scan(result, now=1100)
    assert ig.source(source["id"])["completed_through"] == 1000


def test_deep_full_scan_checks_both_new_heads_without_losing_tail_progress(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    ig = InstagramStore(Store(tmp_path))
    source = ig.add_source("pets", now=100)
    clock = [1000]
    monkeypatch.setattr(target.time, "time", lambda: clock[0])
    values = [sdk_post(f"OldP{i}", 10) for i in range(1000)]
    reels = [sdk_post(f"OldR{i}", 10) for i in range(1000)]
    seen = []
    install_scan(monkeypatch, target, values, reels=reels, seen=seen)
    folder = tmp_path / "scan"
    atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    first = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert not first.complete
    ig.apply_scan(first, now=1000)
    values.insert(0, sdk_post("NewPost", 1100))
    reels.insert(0, sdk_post("NewReel", 1100))
    clock[0] = 1300
    seen.clear()
    source = ig.source(source["id"])
    atomic_json(folder / "request.json", {"run_id": "b" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    second = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert {p.shortcode for p in second.posts} == {"NewPost", "NewReel"}
    assert ("posts", "OldP150") in seen and ("reels", "OldR150") in seen
    assert len(seen) == 200 and not second.complete
    assert second.started_at == 1000
    ig.apply_scan(second, now=1300)
    assert ig.source(source["id"])["completed_through"] is None


def test_legacy_full_cursor_resumes_even_with_recent_watermark(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    source = InstagramStore(Store(tmp_path)).add_source("pets", now=100)
    monkeypatch.setattr(target.time, "time", lambda: 1000)
    values = [sdk_post(f"V{i}", 200) for i in range(205)]
    install_scan(monkeypatch, target, values)
    folder = tmp_path / "scan"
    atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    first = ScanBatch.model_validate_json((folder / "result.json").read_text())
    source["cursor"] = {key: first.cursor[key] for key in ("feeds", "done", "next", "started_at")}
    source["completed_through"] = 900
    target.scan_source(tmp_path, source, folder)
    second = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert second.complete and second.started_at == 1000
    assert {p.shortcode for p in [*first.posts, *second.posts]} == {f"V{i}" for i in range(205)}


def test_full_continuation_probes_a_previously_exhausted_feed(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    source = InstagramStore(Store(tmp_path)).add_source("pets", now=100)
    values = [sdk_post(f"Old{i}", 10) for i in range(205)]
    reels = []
    install_scan(monkeypatch, target, values, reels=reels)
    folder = tmp_path / "scan"
    atomic_json(folder / "request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, folder)
    first = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert not first.complete and first.cursor["done"] == ["reels"]
    source["cursor"] = first.cursor
    reels.append(sdk_post("NewReel", 200))
    target.scan_source(tmp_path, source, folder)
    second = ScanBatch.model_validate_json((folder / "result.json").read_text())
    assert second.complete and {p.shortcode for p in second.posts} == {"NewReel"}


def test_renamed_source_never_switches_numeric_identity(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    source = InstagramStore(Store(tmp_path)).add_source("oldname", now=100)
    source["instagram_id"] = "123"
    install_scan(monkeypatch, target, [sdk_post("A")], pinned=True)
    atomic_json(tmp_path / "scan/request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, tmp_path / "scan")
    result = ScanBatch.model_validate_json((tmp_path / "scan/result.json").read_text())
    assert result.account_id == "123" and result.username == "renamed"


def test_login_is_atomic_private_and_redacted(tmp_path, monkeypatch, capsys):
    import instaloader

    import shorts_local.instagram as target

    store = Store(tmp_path)
    auth = tmp_path / "auth/instagram.json"
    atomic_json(auth, {"username": "old", "session": {}, "generation": "old"})
    before = auth.read_bytes()
    fake = SimpleNamespace(context=SimpleNamespace(), close=lambda: None)

    def fail(username, password):
        raise instaloader.BadCredentialsException("secret-password sessionid=SECRET")

    fake.login = fail
    monkeypatch.setattr(target, "_loader", lambda root, **kwargs: fake)
    with pytest.raises(target.InstagramError) as exc:
        target.login_instagram(store, "pets", prompt_secret=lambda _: "secret-password")
    assert exc.value.code == "auth_wait" and auth.read_bytes() == before
    assert "SECRET" not in str(exc.value) + capsys.readouterr().out
    fake.login = lambda username, password: None
    fake.test_login = lambda: "pets"
    fake.save_session = lambda: {"sessionid": "fixture-session"}
    assert target.login_instagram(store, "pets", prompt_secret=lambda _: "secret-password") == "pets"
    assert auth.stat().st_mode & 0o777 == 0o600
    assert "secret-password" not in auth.read_text()
    assert json.loads(auth.read_text())["generation"] != "old"


def test_redirects_size_and_timeout_are_bounded(tmp_path, monkeypatch):
    import shorts_local.instagram as target

    client_class = httpx.Client
    calls = []

    def request(req):
        calls.append(req)
        assert "cookie" not in req.headers
        return httpx.Response(302, headers={"location": "https://evil.test/capability"})

    monkeypatch.setattr(target.httpx, "Client", lambda **kw: client_class(
        transport=httpx.MockTransport(request), **kw))
    out = tmp_path / "source.partial"
    with pytest.raises(target.InstagramError):
        target._download_media("https://video.cdninstagram.com/a", out, 30)
    assert len(calls) == 1 and not out.exists()
    for url in ("http://video.cdninstagram.com/a", "https://cdninstagram.com.evil.test/a",
                "https://x@video.fbcdn.net/a", "https://video.fbcdn.net:444/a"):
        with pytest.raises(target.InstagramError):
            target._download_media(url, out, 30)
    monkeypatch.setattr(target, "MAX_SOURCE_BYTES", 16)
    monkeypatch.setattr(target.httpx, "Client", lambda **kw: client_class(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 17)), **kw))
    with pytest.raises(target.InstagramError) as exc:
        target._download_media("https://video.fbcdn.net/a", out, 30)
    assert exc.value.code == "unsupported"


@pytest.mark.parametrize("kind", ["GraphImage", "GraphSidecar"])
def test_fetch_rejects_unsupported_or_invalid_media(tmp_path, monkeypatch, kind):
    import instaloader

    import shorts_local.instagram as target

    Store(tmp_path)
    monkeypatch.setattr(target, "_loader", lambda root: SimpleNamespace(context=None, close=lambda: None))
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda ctx, code: sdk_post(code, kind=kind))
    value = InstagramInput(item_id="a" * 12, shortcode="Ab", url=post().url)
    with pytest.raises(target.InstagramError) as exc:
        target._fetch_media(tmp_path, value, tmp_path, Settings())
    assert exc.value.code == "unsupported"
    assert not (tmp_path / "source.mp4").exists()


@pytest.mark.parametrize("duration", [0.99, 600.01])
def test_fetch_checks_duration_before_download(tmp_path, monkeypatch, duration):
    import instaloader

    import shorts_local.instagram as target

    Store(tmp_path)
    value = sdk_post("Ab")
    value.video_duration = duration
    monkeypatch.setattr(target, "_loader", lambda root: SimpleNamespace(context=None, close=lambda: None))
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda ctx, code: value)
    with pytest.raises(target.InstagramError) as exc:
        target._fetch_media(tmp_path, InstagramInput(item_id="a" * 12, shortcode="Ab", url=post().url),
                            tmp_path, Settings())
    assert exc.value.code == "unsupported"


def test_sdk_errors_are_classified_without_raw_text(tmp_path):
    import instaloader

    import shorts_local.instagram as target

    for error, code in ((instaloader.LoginRequiredException, "auth_wait"),
                        (instaloader.TooManyRequestsException, "rate_limit"),
                        (instaloader.ProfileNotExistsException, "unavailable"),
                        (instaloader.ConnectionException, "transient")):
        classified = target.classify_error(error("signed-url=SECRET"))
        assert classified.code == code and "SECRET" not in str(classified)
        if code == "rate_limit":
            assert classified.retry_at >= time.time() + 3599


@pytest.mark.parametrize("duration", [0.99, 600.01])
def test_downloaded_duration_limit_marks_material_unsupported(tmp_path, monkeypatch, duration):
    import instaloader

    import shorts_local.instagram as target

    Store(tmp_path)
    monkeypatch.setattr(target, "_loader", lambda root: SimpleNamespace(context=None, close=lambda: None))
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda ctx, code: sdk_post(code))
    client_class = httpx.Client
    monkeypatch.setattr(target.httpx, "Client", lambda **kw: client_class(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"media")), **kw))
    monkeypatch.setattr(target, "probe", lambda path: {
        "format": {"duration": str(duration)},
        "streams": [{"codec_type": "video", "width": 640, "height": 360}],
    })
    with pytest.raises(target.InstagramError) as exc:
        target._fetch_media(tmp_path, InstagramInput(item_id="a" * 12, shortcode="Ab", url=post().url),
                            tmp_path, Settings())
    assert exc.value.code == "unsupported"
    assert not (tmp_path / "source.mp4").exists()


def test_adapter_uses_pinned_sdk_feed_and_session_contract(tmp_path, monkeypatch):
    import instaloader

    import shorts_local.instagram as target

    s = Store(tmp_path)
    source = InstagramStore(s).add_source("pets", now=100)
    loader = target._loader(tmp_path)
    loader.load_session("pets", {"sessionid": "fixture-only", "csrftoken": "fixture-csrf"})
    assert loader.save_session()["sessionid"] == "fixture-only"
    profile = instaloader.Profile(loader.context, {"id": "123", "username": "pets"})
    profile._has_full_metadata = True

    def graph(doc_id, variables, referer):
        if "target_user_id" in variables["data"]:
            assert variables["data"]["target_user_id"] == "123"
            key, edges = "xdt_api__v1__clips__user__connection_v2", [{"node": {"media": {"code": "Ab"}}}]
        else:
            key, edges = "xdt_api__v1__feed__user_timeline_graphql_connection", []
        return {"data": {key: {"edges": edges, "page_info": {"has_next_page": False}}}}

    monkeypatch.setattr(loader.context, "doc_id_graphql_query", graph)
    monkeypatch.setattr(instaloader.Profile, "from_username", lambda context, name: profile)
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda context, shortcode: sdk_post(shortcode))
    monkeypatch.setattr(target, "_loader", lambda root: loader)
    atomic_json(tmp_path / "scan/request.json", {"run_id": "a" * 32, "source": source})
    target.scan_source(tmp_path, source, tmp_path / "scan")
    result = ScanBatch.model_validate_json((tmp_path / "scan/result.json").read_text())
    assert result.complete and result.error_code is None
    assert [p.shortcode for p in result.posts] == ["Ab"]


@pytest.mark.parametrize("status,body,location,code", [
    (429, {"status": "fail", "message": "Please wait"}, None, "rate_limit"),
    (302, {}, "https://www.instagram.com/accounts/login/", "auth_wait"),
    (400, {"status": "fail", "message": "checkpoint_required"}, None, "auth_wait"),
    (400, {"status": "fail", "message": "challenge_required"}, None, "auth_wait"),
    (400, {"status": "fail", "message": "feedback_required"}, None, "rate_limit"),
])
def test_real_sdk_response_errors_preserve_shared_wait(tmp_path, monkeypatch, status, body, location, code):
    import requests

    import shorts_local.instagram as target

    store = Store(tmp_path)
    ig = InstagramStore(store)
    source = ig.add_source("pets")
    loader = target._loader(tmp_path)
    loader.context.username = "pets"
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    response.url = "https://www.instagram.com/api/v1/users/web_profile_info/?sessionid=SECRET"
    if location:
        response.headers["Location"] = location
    monkeypatch.setattr(loader.context._session, "get", lambda *a, **kw: response)
    try:
        with pytest.raises(Exception) as exc:
            loader.context.get_json("api/v1/users/web_profile_info/", params={"username": "pets"})
        error = target.classify_error(exc.value)
        assert error.code == code and "SECRET" not in str(error)
        ig.record_failure(source["id"], error.code, retry_at=error.retry_at)
        if code == "rate_limit":
            assert store.value("instagram_next_run") >= time.time() + 3599
        else:
            assert store.value("instagram_auth_wait") is True
    finally:
        loader.close()


def test_fetch_sdk_versions_never_issue_unguarded_head_requests(tmp_path, monkeypatch):
    import instaloader
    import requests

    import shorts_local.instagram as target
    from tests.instagram_helpers import make_source

    make_source(tmp_path / "fixture")
    payload = (tmp_path / "fixture/source.mp4").read_bytes()
    loader = target._loader(tmp_path)
    loader.context.username = "pets"
    first, second = "https://video.cdninstagram.com/first", "https://video.fbcdn.net/second"
    post = instaloader.Post(loader.context, {
        "id": "1234", "shortcode": "Ab", "__typename": "GraphVideo", "is_video": True,
        "owner": {"id": "123", "username": "pets"}, "date": 200, "caption": "fixture",
        "video_duration": 2, "video_url": first,
        "iphone_struct": {"video_versions": [{"url": second}]},
    })
    unchecked = []
    def respond(adapter, request, **kwargs):
        unchecked.append(request.url)
        response = requests.Response()
        response.request, response.url, response._content = request, request.url, b""
        response.status_code = 302 if request.url == second else 200
        response.headers["Content-Length"] = "200" if request.url.startswith("http:") else "100"
        if response.status_code == 302:
            response.headers["Location"] = "http://127.0.0.1:1234/internal"
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", respond)
    monkeypatch.setattr(target, "_loader", lambda root: loader)
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda context, shortcode: post)
    client_class = httpx.Client
    downloads = []
    def download(request):
        downloads.append(str(request.url))
        assert "cookie" not in request.headers
        return httpx.Response(200, content=payload)
    monkeypatch.setattr(target.httpx, "Client", lambda **kw: client_class(
        transport=httpx.MockTransport(download), **kw))
    media = target._fetch_media(tmp_path, InstagramInput(item_id="a" * 12, shortcode="Ab", url="https://instagram.com/p/Ab/"),
                                tmp_path, Settings())
    assert media.duration == 2
    assert unchecked == []
    assert downloads == [first]
