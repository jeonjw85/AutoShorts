import json

import pytest
from pydantic import ValidationError


def test_fenced_json_with_braces_inside_text(story_data):
    from shorts_local.planning import parse_story

    story_data["description"] = "작은 {상자}가 등장하는 창작 이야기입니다."
    story = parse_story("```json\n" + json.dumps(story_data) + "\n```")
    assert story.title == "내일 도착한 편지"
    assert "{상자}" in story.description


def test_reasoning_json_is_not_mistaken_for_the_final_script(story_data):
    from shorts_local.planning import parse_story

    draft = {**story_data, "title": "버릴 초안"}
    text = "<think>\n" + json.dumps(draft) + "\n</think>\n" + json.dumps(story_data)
    assert parse_story(text).title == "내일 도착한 편지"
    with pytest.raises(ValueError):
        parse_story("<think>unfinished reasoning " + json.dumps(draft))


def test_korean_pipeline_rejects_wrong_language_assets(story_data):
    from shorts_local.models import Story

    story_data["scenes"][0]["image_prompt"] = "비 내리는 골목의 빨간 우체통 그림"
    with pytest.raises(ValidationError):
        Story.model_validate(story_data)
    story_data["scenes"][0]["image_prompt"] = "A red mailbox in the rain"
    story_data["scenes"][0]["narration"] = "This narration is in the wrong language."
    with pytest.raises(ValidationError):
        Story.model_validate(story_data)


def test_bad_or_repetitive_scripts_are_not_renderable(story_data):
    from shorts_local.planning import parse_story

    with pytest.raises(ValueError):
        parse_story("오늘은 영상 아이디어가 없습니다.")
    story_data["scenes"][1]["narration"] = story_data["scenes"][0]["narration"]
    with pytest.raises(ValidationError):
        parse_story(json.dumps(story_data))


def test_title_normalization_catches_spacing_and_punctuation():
    from shorts_local.planning import same_title

    assert same_title("내일 도착한 편지!", "내일도착한편지")
    assert not same_title("내일 도착한 편지", "내일 도착한 소포")


@pytest.mark.parametrize("status", ["Logged in using an API key", "Not logged in", ""])
def test_api_key_auth_is_not_used_for_subscription_mode(status):
    from shorts_local.planning import require_subscription
    from shorts_local.process import BlockedError

    with pytest.raises(BlockedError):
        require_subscription(0, status)


def test_subscription_environment_drops_paid_credentials():
    from shorts_local.planning import subscription_env

    env = subscription_env({"PATH": "/bin", "OPENAI_API_KEY": "secret",
                            "CODEX_API_KEY": "secret", "OPENAI_BASE_URL": "https://paid.test"})
    assert env == {"PATH": "/bin"}


def test_completed_voice_checkpoint_requires_every_nonempty_wav(tmp_path, story_data):
    from shorts_local.engines import speech_complete
    from shorts_local.models import Story

    (tmp_path / "voice").mkdir()
    (tmp_path / "voice/000.wav").write_bytes(b"broken")
    assert not speech_complete(Story(**story_data), tmp_path)
