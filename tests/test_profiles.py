import pytest

from shorts_local.store import Store


def test_profile_settings_are_independent(tmp_path):
    s = Store(tmp_path)
    original = s.profile().settings
    s.add_profile("animals", "귀여운 동물")
    s.update_profile_settings("animals", {"topic": "동물 이야기"})
    assert s.profile("animals").settings.topic == "동물 이야기"
    assert s.profile().settings == original
    with pytest.raises(ValueError):
        s.update_profile_settings("animals", {"planner": "paid-api"})
    assert s.profile("animals").settings.planner == "mlx"
    s.set_paused(True, channel_id="animals")
    assert s.profile("animals").paused
    assert not s.profile().paused
    assert not s.value("paused", False)


@pytest.mark.parametrize("channel", ["../other", "A", "a" * 33, "", "a\n"])
def test_profile_ids_and_unknown_channels_are_rejected(tmp_path, channel):
    s = Store(tmp_path)
    with pytest.raises(ValueError):
        s.add_profile(channel, "새 채널")
    with pytest.raises(ValueError):
        s.profile("missing")
    assert [p.id for p in s.profiles()] == ["default"]
    assert not (tmp_path / "channels").exists()


def test_nested_transaction_rolls_back_profile_changes(tmp_path):
    s = Store(tmp_path)
    with pytest.raises(RuntimeError), s.transaction():
        s.add_profile("animals", "동물")
        s.update_profile_settings("animals", {"topic": "동물 이야기"})
        raise RuntimeError("abort command")
    assert [p.id for p in s.profiles()] == ["default"]
