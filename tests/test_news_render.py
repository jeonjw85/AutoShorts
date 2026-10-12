import json
import math
import struct
import wave

import pytest
from PIL import Image, ImageDraw

from shorts_local.config import Settings
from shorts_local.render import validate_video
from tests.news_helpers import article, plan_data


def tones(folder, count=4):
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(count):
        path = folder / f"{i:03}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            stream.writeframes(b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * n / 24000)))
                                       for n in range(12000)))
        files.append(path)
    return files


@pytest.mark.parametrize("with_image", [False, True])
def test_news_video_renders_source_date_and_exact_captions(tmp_path, monkeypatch, with_image):
    from shorts_local.news_plan import NewsPlan, validate_news_plan
    from shorts_local.news_render import render_news_video

    settings = Settings(width=360, height=640, min_seconds=1)
    plan = validate_news_plan(NewsPlan.model_validate(plan_data()), article(), settings)
    drawn = []
    original = ImageDraw.ImageDraw.text

    def text(self, xy, value, *args, **kwargs):
        drawn.append(value)
        return original(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (720, 480), "#4488aa").save(photo)
    video = render_news_video(plan, article(), tones(tmp_path / "voice"), tmp_path,
                              settings, image=photo if with_image else None)
    report = validate_video(video, settings, full_decode=True)
    assert report["width"] == 360 and report["height"] == 640
    assert drawn.count("출처  서울뉴스") == 4
    assert drawn.count("2026-10-12") == 4
    captions = (tmp_path / "captions.srt").read_text()
    assert all(c["narration"] in captions for c in plan_data()["cards"])
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert "https://example.com/a" in metadata["description"]
    with Image.open(tmp_path / "cover.png") as frame:
        assert len(frame.crop((20, 492, 330, 529)).getcolors(100000)) > 10
