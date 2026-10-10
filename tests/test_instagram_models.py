import pytest
from pydantic import ValidationError

from shorts_local.config import Settings


def test_legacy_settings_get_instagram_defaults():
    settings = Settings.model_validate({"topic": "이전 설정의 이야기"})
    assert settings.production_type == "story"
    assert settings.instagram_edit_mode == "narrated"
    assert settings.instagram_poll_minutes == 60
    assert settings.instagram_accent_color == "#E8B257"
    for change in ({"instagram_poll_minutes": 14}, {"instagram_poll_minutes": 1441},
                   {"instagram_style": ""}, {"instagram_style": "a" * 501},
                   {"instagram_accent_color": "red"}, {"instagram_edit_mode": "bad"},
                   {"production_type": "bad"}):
        with pytest.raises(ValidationError):
            Settings(**change)


def test_url_and_username_canonicalization():
    from shorts_local.instagram_models import normalize_username, parse_instagram_url

    expected = ("Ab_12-", "https://www.instagram.com/reel/Ab_12-/")
    for url in ("https://instagram.com/p/Ab_12-/?igsh=x#y",
                "https://www.instagram.com:443/reel/Ab_12-/"):
        assert parse_instagram_url(url) == expected
    assert normalize_username(" @Pet.Name ") == "pet.name"
    for value in ("", "@", "a" * 31, "@@a", "../other/", "동물", "pet name"):
        with pytest.raises(ValueError):
            normalize_username(value)
    for url in ("http://instagram.com/reel/x/", "https://me:secret@instagram.com/reel/x/",
                "https://instagram.com.evil.test/reel/x/", "https://instagram.com:444/p/x/",
                "https://instagram.com/p/%2f/", "https://instagram.com/reel/",
                "https://instagram.com/p/x/more", "https://insta\tgram.com/p/x/"):
        with pytest.raises(ValueError) as exc:
            parse_instagram_url(url)
        assert "secret" not in str(exc.value)


def test_input_and_source_boundaries():
    from shorts_local.instagram_models import InstagramInput, InstagramPost, SourceMedia

    data = {"item_id": "a" * 12, "shortcode": "Ab", "url": "https://instagram.com/p/Ab/"}
    item = InstagramInput(**data)
    assert item.url == "https://www.instagram.com/reel/Ab/"
    with pytest.raises(ValidationError):
        item.shortcode = "Other"
    for change in ({"item_id": "../other"}, {"source_id": "bad"},
                   {"url": "https://instagram.com/p/Other/"}, {"cookie": "secret"}):
        with pytest.raises(ValidationError):
            InstagramInput(**(data | change))
    post = InstagramPost(shortcode="Ab", url=data["url"], author="Pet", owner_id="123",
                         published_at=100, caption="영상", media_type="video")
    base = dict(post=post, sha256="a" * 64, bytes=300 * 1024**2, duration=600,
                width=640, height=360, has_audio=True)
    assert SourceMedia(**base).duration == 600
    for change in ({"duration": float("nan")}, {"duration": 0.99}, {"duration": 600.01},
                   {"bytes": 300 * 1024**2 + 1}, {"width": 0}):
        with pytest.raises(ValidationError):
            SourceMedia(**(base | change))
