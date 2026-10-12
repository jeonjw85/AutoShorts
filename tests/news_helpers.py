from datetime import UTC, datetime

BODY = ("서울시는 버스 20대를 도입했다고 밝혔습니다. "
        "새 버스는 다음 달부터 운행할 예정입니다. "
        "시는 시민들의 이동 편의를 높이겠다고 설명했습니다. "
        "정류장 안내문과 운행 시간표도 함께 바뀔 예정입니다. "
        "시 관계자는 구체적인 노선은 추가 안내한다고 말했습니다. ") * 4
DATE = datetime(2026, 10, 11, 23, tzinfo=UTC).timestamp()


def html(body=BODY, date="2026-10-11T23:00:00Z", image=""):
    return (f'<html><head><title>새 버스 도입</title><meta property="og:site_name" content="서울뉴스">'
            f'<meta property="article:published_time" content="{date}">'
            f'<meta property="og:image" content="{image}"></head>'
            f'<body><nav>제외할 메뉴</nav><article><p>{body}</p></article></body></html>')


def article():
    from shorts_local.news_fetch import extract_article

    return extract_article("https://example.com/a", html(), rss_published_at=None)


def plan_data():
    return {"title": "서울시 버스 도입", "description": "서울시 버스 도입 소식",
            "cards": [
                {"lines": ["서울시는 버스", "20대를 도입"],
                 "narration": "서울시는 버스 20대를 도입했다고 밝혔습니다.",
                 "evidence": "서울시는 버스 20대를 도입했다고 밝혔습니다.", "highlight": "20대"},
                {"lines": ["새 버스는", "다음 달부터 운행"],
                 "narration": "새 버스는 다음 달부터 운행할 예정입니다.",
                 "evidence": "새 버스는 다음 달부터 운행할 예정입니다.", "highlight": None},
                {"lines": ["시민들의", "이동 편의를"],
                 "narration": "시는 시민들의 이동 편의를 높이겠다고 설명했습니다.",
                 "evidence": "시는 시민들의 이동 편의를 높이겠다고 설명했습니다.", "highlight": None},
                {"lines": ["정류장 안내문과", "운행 시간표도"],
                 "narration": "정류장 안내문과 운행 시간표도 함께 바뀔 예정입니다.",
                 "evidence": "정류장 안내문과 운행 시간표도 함께 바뀔 예정입니다.", "highlight": None},
            ]}
