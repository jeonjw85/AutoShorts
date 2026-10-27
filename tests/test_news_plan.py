import json

import pytest

from shorts_local.config import Settings
from shorts_local.process import BlockedError
from tests.news_helpers import article, plan_data


@pytest.mark.parametrize("field,bad", [
    ("narration", "서울시는 버스 12대를 도입했습니다."),
    ("evidence", "본문에 없는 인용입니다."),
    ("highlight", "20명"),
    ("lines", ["확인 안 된 12대", "도입 발표"]),
])
def test_numbers_and_evidence_are_grounded(field, bad):
    from shorts_local.news_plan import NewsPlan, validate_news_plan

    data = plan_data()
    good = validate_news_plan(NewsPlan.model_validate(data), article(), Settings())
    assert "2026-10-12" in good.description
    assert "https://example.com/a" in good.description
    data["cards"][0][field] = bad
    with pytest.raises(ValueError):
        validate_news_plan(NewsPlan.model_validate(data), article(), Settings())


def test_semantic_failure_replans_once_and_stays_blocked_across_retry(tmp_path, monkeypatch):
    from shorts_local import news_plan

    calls = []

    def draft(settings, prompt, directory, *, schema, **kwargs):
        calls.append(schema["title"])
        assert "ORIGINAL fictional" not in prompt
        assert article().body in json.loads(prompt.split("SOURCE_DATA:\n")[-1])["article"]["body"]
        if schema["title"] == "GroundingVerdict":
            return json.dumps({"supported": False, "issues": ["본문에 없는 인물과 결과"]})
        data = plan_data()
        data["cards"][0]["narration"] = "서울시는 김철수가 새 버스를 운전했다고 밝혔습니다."
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(news_plan, "generate_draft", draft)
    for _ in range(2):
        with pytest.raises(BlockedError):
            news_plan.plan_news(Settings(), article(), tmp_path)
    assert calls == ["NewsPlan", "GroundingVerdict", "NewsPlan", "GroundingVerdict"]
    assert not (tmp_path / "news-plan.json").exists()
    assert not (tmp_path / "story.json").exists()


def test_accepted_plan_cache_is_bound_to_article_and_settings(tmp_path, monkeypatch):
    from shorts_local import news_plan

    calls = []

    def draft(settings, prompt, directory, *, schema, **kwargs):
        calls.append(schema["title"])
        return json.dumps({"supported": True, "issues": []} if schema["title"] == "GroundingVerdict" else plan_data())

    monkeypatch.setattr(news_plan, "generate_draft", draft)
    first = news_plan.plan_news(Settings(), article(), tmp_path)
    assert news_plan.plan_news(Settings(), article(), tmp_path) == first
    assert len(calls) == 2
    news_plan.plan_news(Settings(news_style="차분하게 전달"), article(), tmp_path)
    assert len(calls) == 4


def test_plan_failure_uses_a_dedicated_exception(tmp_path, monkeypatch):
    from shorts_local import news_plan
    from shorts_local.news_plan import NewsPlanError

    monkeypatch.setattr(news_plan, "generate_draft", lambda *a, **k: "{}")
    with pytest.raises(NewsPlanError):
        news_plan.plan_news(Settings(), article(), tmp_path)
