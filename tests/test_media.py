import math
import struct
import wave

import pytest


def write_tone(path, seconds=0.8):
    with wave.open(str(path), "w") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(i * 440 * math.tau / 24000)))
                                   for i in range(int(24000 * seconds))))


def test_korean_wrapping_keeps_every_character_and_fits_width():
    from shorts_local.artwork import font, wrap_text

    text = "아주길고공백없는한국어문장도화면밖으로삐져나가면안됩니다"
    face = font(42)
    lines = wrap_text(text, face, 240)
    assert "".join(lines) == text
    assert len(lines) > 1
    assert all(face.getlength(line) <= 240 for line in lines)


def test_caption_wrap_preserves_korean_words_when_they_fit():
    from shorts_local.artwork import font, wrap_text

    text = "비가 그친 골목에서 내 이름을 발견했다."
    face = font(42)
    lines = wrap_text(text, face, 300)
    assert " ".join(lines) == text
    assert all(word in text.split() for line in lines for word in line.split())
    assert all(face.getlength(line) <= 300 for line in lines)


def test_real_ffmpeg_renders_korean_portrait_video(tmp_path, story_data):
    from shorts_local.config import Settings
    from shorts_local.models import Story
    from shorts_local.render import render_video, validate_video

    (tmp_path / "voice").mkdir()
    for index in range(3):
        write_tone(tmp_path / "voice" / f"{index:03}.wav")
    settings = Settings(width=360, height=640, min_seconds=1)
    output = render_video(Story(**story_data), tmp_path, settings)
    result = validate_video(output, settings)
    assert result["width"] == 360
    assert result["height"] == 640
    assert 2.4 < result["duration"] < 4
    assert result["audio_codec"] == "aac"
    assert (tmp_path / "captions.srt").read_text().count(" --> ") == 3
    assert (tmp_path / "cover.png").stat().st_size > 1000


def test_empty_audio_cannot_produce_a_video(tmp_path, story_data):
    from shorts_local.config import Settings
    from shorts_local.models import Story
    from shorts_local.render import render_video

    (tmp_path / "voice").mkdir()
    for index in range(3):
        write_tone(tmp_path / "voice" / f"{index:03}.wav", seconds=0)
    with pytest.raises(ValueError, match="음성"):
        render_video(Story(**story_data), tmp_path, Settings())


@pytest.mark.parametrize("damage", ["header-only", "silent"])
def test_invalid_audio_payload_is_rejected_before_ffmpeg(tmp_path, story_data, damage):
    from shorts_local.config import Settings
    from shorts_local.engines import speech_complete
    from shorts_local.models import Story
    from shorts_local.render import render_video

    (tmp_path / "voice").mkdir()
    story = Story(**story_data)
    for index in range(3):
        path = tmp_path / "voice" / f"{index:03}.wav"
        write_tone(path, seconds=6)
        payload = path.read_bytes()
        path.write_bytes(payload[:44] if damage == "header-only"
                         else payload[:44] + bytes(len(payload) - 44))
    assert not speech_complete(story, tmp_path)
    with pytest.raises(ValueError, match="음성"):
        render_video(story, tmp_path, Settings())
    assert not (tmp_path / "video.mp4").exists()
