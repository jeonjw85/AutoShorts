import hashlib
import json
import subprocess
from array import array

import pytest
from PIL import Image

from shorts_local.config import Settings
from shorts_local.editing_models import Analysis, EditPlan, Transcript
from shorts_local.media import probe
from tests.instagram_helpers import make_source, media
from tests.test_media import write_tone


def edit(mode="original", cuts=((0, 2),), segments=()):
    analysis = Analysis(transcript=Transcript(language="en", segments=list(segments)),
                        observations=[{"id": "v0", "time": 0, "text": "Colored corners are visible."}])
    plan = EditPlan(title="화면 전체를 담은 영상", description="원본 화면과 소리를 확인합니다.", mode=mode,
                    clips=[{"start": start, "end": end, "evidence_ids": ["v0"],
                            "narration": "원본 화면의 움직임을 살펴보세요." if mode == "narrated" else None}
                           for start, end in cuts],
                    translations=[{"segment_id": s["id"], "text": "화면을 확인하세요."} for s in segments
                                  if any(s["end"] > a and s["start"] < b for a, b in cuts)])
    return plan, analysis


def tone_at(path, start, seconds=0.3):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
                             "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                            check=True, capture_output=True)
    samples = array("h", result.stdout)
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:], strict=False))
    return crossings / (len(samples) / 16000), max(map(abs, samples), default=0)


def marked_source(folder, *, rotated=False):
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "raw.mp4"
    source = folder / "source.mp4"
    video = ("color=c=gray:size=320x180:rate=30,"
             "drawbox=x=0:y=0:w=30:h=30:color=red:t=fill,"
             "drawbox=x=290:y=0:w=30:h=30:color=lime:t=fill,"
             "drawbox=x=0:y=150:w=30:h=30:color=blue:t=fill,"
             "drawbox=x=290:y=150:w=30:h=30:color=white:t=fill")
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", video]
    if rotated:
        args += ["-itsoffset", "0.4"]
    args += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-t", "2"]
    if rotated:
        args += ["-vf", "select='if(lt(t,1),not(mod(n,2)),1)'", "-fps_mode", "vfr", "-output_ts_offset", "5"]
    subprocess.run([*args, "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(raw)],
                   check=True, capture_output=True)
    if rotated:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(raw), "-c", "copy",
                        "-metadata:s:v:0", "rotate=90", str(source)], check=True, capture_output=True)
    else:
        raw.replace(source)
    info = probe(source)
    origin = float(info["format"].get("start_time", 0))
    duration = max(float(s.get("start_time", origin)) + float(s["duration"]) for s in info["streams"]) - origin
    return media(duration=duration, bytes=source.stat().st_size, width=320, height=180,
                 sha256=hashlib.sha256(source.read_bytes()).hexdigest())


def frame(path, target, when=1):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(when), "-i", str(path),
                    "-frames:v", "1", str(target)], check=True, capture_output=True)
    return Image.open(target).convert("RGB")


def assert_corners(source_image, output_image):
    w, h = source_image.size
    scale = min(360 / w, 352 / h)
    left, top = (360 - w * scale) / 2, 128 + (352 - h * scale) / 2
    for x, y in ((15, 15), (w - 15, 15), (15, h - 15), (w - 15, h - 15)):
        expected = source_image.getpixel((x, y))
        actual = output_image.getpixel((round(left + x * scale), round(top + y * scale)))
        assert max(abs(a - b) for a, b in zip(actual, expected, strict=True)) < 35


def test_original_cuts_map_speech_to_output_frames(tmp_path):
    from shorts_local.import_render import build_timeline, render_imported_video

    source = make_source(tmp_path, seconds=10)
    # Distinct frequencies make a wrong audio cut observable, rather than only
    # proving that some original sound survived.
    replaced = tmp_path / "frequencies.mp4"
    signal = "aevalsrc=if(between(t\\,2\\,4)\\,sin(2*PI*660*t)\\,if(between(t\\,7\\,9)\\,sin(2*PI*880*t)\\,sin(2*PI*330*t))):s=48000"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "source.mp4"),
                    "-f", "lavfi", "-i", signal, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-t", "10", str(replaced)], check=True, capture_output=True)
    replaced.replace(tmp_path / "source.mp4")
    source.sha256 = hashlib.sha256((tmp_path / "source.mp4").read_bytes()).hexdigest()
    source.bytes = (tmp_path / "source.mp4").stat().st_size
    plan, analysis = edit(cuts=((2, 4), (7, 9)), segments=(
        {"id": "outside", "start": 0.1, "end": 0.8, "text": "outside"},
        {"id": "a", "start": 2.5, "end": 3.3, "text": "first"},
        {"id": "b", "start": 7.2, "end": 8.1, "text": "second"},
    ))
    settings = Settings(width=360, height=640, min_seconds=1, instagram_edit_mode="original")
    timeline = build_timeline(plan, source, analysis, settings, [], workdir=tmp_path)
    assert [(c.start_frame, c.end_frame) for c in timeline.captions] == [(15, 39), (66, 93)]
    assert timeline.frames == 120 and all(c.speed == 1 for c in timeline.clips)
    output = render_imported_video(source, plan, analysis, tmp_path, settings, [])
    assert tone_at(output, 0.5)[0] == pytest.approx(660, abs=5)
    assert tone_at(output, 2.5)[0] == pytest.approx(880, abs=5)
    assert (tmp_path / "captions.srt").read_text().count(" --> ") == 2


def test_narration_speed_limits_and_short_source(tmp_path):
    from shorts_local.import_render import NarrationTimingError, build_timeline

    source = media(duration=2)
    plan, analysis = edit("narrated")
    settings = Settings(width=360, height=640)
    voice = tmp_path / "voice.wav"
    for duration, speed in ((2.5, 0.8), (1.6, 1.25)):
        write_tone(voice, duration)
        options = settings.model_copy(update={"min_seconds": 1})
        timeline = build_timeline(plan, source, analysis, options, [voice], workdir=tmp_path)
        assert timeline.clips[0].speed == pytest.approx(speed)
    for duration in (1.5, 2.6):
        write_tone(voice, duration)
        with pytest.raises(NarrationTimingError) as exc:
            build_timeline(plan, source, analysis, settings, [voice], workdir=tmp_path)
        assert exc.value.durations == [duration]
    plan, analysis = edit()
    assert build_timeline(plan, source, analysis, settings.model_copy(update={"instagram_edit_mode": "original"}),
                          [], workdir=tmp_path).frames == 60
    plan, analysis = edit(cuts=((0, 16),))
    with pytest.raises(ValueError):
        build_timeline(plan, media(duration=16), analysis, Settings(min_seconds=1, max_seconds=15,
                       instagram_edit_mode="original"), [], workdir=tmp_path)


@pytest.mark.parametrize("mode", ["original", "narrated"])
def test_render_modes_keep_full_frame_and_valid_audio(tmp_path, mode):
    from shorts_local.import_render import render_imported_video
    from shorts_local.render import validate_video

    source = marked_source(tmp_path)
    plan, analysis = edit(mode)
    settings = Settings(width=360, height=640, instagram_edit_mode=mode)
    voice = tmp_path / "voice.wav"
    write_tone(voice, 2)
    output = render_imported_video(source, plan, analysis, tmp_path, settings,
                                   [voice] if mode == "narrated" else [])
    result = validate_video(output, settings, min_seconds=2, full_decode=True)
    assert probe(output)["streams"][0]["sample_aspect_ratio"] == "1:1"
    assert result["duration"] <= 2.1 and result["audio_codec"] == "aac"
    assert tone_at(output, 0.5)[0] == pytest.approx(440 if mode == "narrated" else 880, abs=5)
    original = frame(tmp_path / "source.mp4", tmp_path / "original.png")
    finished = frame(output, tmp_path / "finished.png")
    assert_corners(original, finished)
    timeline = json.loads((tmp_path / "timeline.json").read_text())
    assert max((c["end_frame"] for c in timeline["captions"]), default=0) <= timeline["frames"]
    assert (tmp_path / "cover.png").exists()
    assert "창작 이야기" not in (tmp_path / "captions.srt").read_text()


def test_rotated_vfr_input_keeps_audio_and_captions_aligned(tmp_path):
    from shorts_local.import_render import render_imported_video

    source = marked_source(tmp_path, rotated=True)
    info = probe(tmp_path / "source.mp4")
    assert float(info["format"]["start_time"]) > 4
    assert info["streams"][0]["avg_frame_rate"] != "30/1"
    plan, analysis = edit(segments=({"id": "speech", "start": 0.6, "end": 1.2, "text": "tone"},))
    output = render_imported_video(source, plan, analysis, tmp_path,
                                   Settings(width=360, height=640, instagram_edit_mode="original"), [])
    assert probe(output)["streams"][0]["avg_frame_rate"] == "30/1"
    assert tone_at(output, 0, 0.2)[1] < 10
    assert tone_at(output, 0.7)[0] == pytest.approx(880, abs=5)
    assert "00:00:00,600 --> 00:00:01,200" in (tmp_path / "captions.srt").read_text()
    assert_corners(frame(tmp_path / "source.mp4", tmp_path / "original.png"),
                   frame(output, tmp_path / "finished.png"))


@pytest.mark.parametrize("silent", [True, False])
def test_speechless_outputs_have_aac_without_dialogue(tmp_path, silent):
    from shorts_local.import_render import render_imported_video

    source = make_source(tmp_path, audio=silent, silent=silent)
    plan, analysis = edit()
    output = render_imported_video(source, plan, analysis, tmp_path,
                                   Settings(width=360, height=640, instagram_edit_mode="original"), [])
    assert tone_at(output, 0.5)[1] == 0
    assert (tmp_path / "captions.srt").read_text() == ""
    assert [s["codec_name"] for s in probe(output)["streams"]] == ["h264", "aac"]


def test_failed_render_does_not_replace_finished_output(tmp_path, monkeypatch):
    import shorts_local.import_render as render

    source = make_source(tmp_path)
    plan, analysis = edit()
    settings = Settings(width=360, height=640, instagram_edit_mode="original")
    (tmp_path / "video.mp4").write_bytes(b"previous-complete-output")
    def fail(*args, **kwargs):
        raise ValueError("full decode failed")
    monkeypatch.setattr(render, "validate_video", fail)
    with pytest.raises(ValueError, match="full decode"):
        render.render_imported_video(source, plan, analysis, tmp_path, settings, [])
    assert (tmp_path / "video.mp4").read_bytes() == b"previous-complete-output"


@pytest.mark.parametrize("mode", ["original", "narrated"])
@pytest.mark.parametrize("cuts", [((0, 15),), ((0, 5), (7, 12), (14, 19))])
def test_exact_maximum_timeline_has_no_encoder_delay_overrun(tmp_path, mode, cuts):
    from shorts_local.import_render import render_imported_video

    source = make_source(tmp_path, seconds=19)
    plan, analysis = edit(mode, cuts=cuts)
    voices = []
    if mode == "narrated":
        for i, (start, end) in enumerate(cuts):
            voice = tmp_path / f"voice-{i}.wav"
            write_tone(voice, end - start)
            voices.append(voice)
    output = render_imported_video(source, plan, analysis, tmp_path,
                                   Settings(width=360, height=640, min_seconds=1, max_seconds=15,
                                            instagram_edit_mode=mode), voices)
    info = probe(output)
    assert float(info["format"]["duration"]) == pytest.approx(15, abs=0.00001)
    assert info["streams"][0]["nb_frames"] == "450"
    for stream in info["streams"]:
        assert float(stream["start_time"]) == 0
        assert float(stream["duration"]) <= 15


def test_fractional_cuts_do_not_accumulate_audio_delay(tmp_path):
    import math
    import wave

    from shorts_local.import_render import render_imported_video

    cuts = ((0.01, 2.42), (3.13, 5.54), (6.27, 8.68), (9.41, 11.82), (12.55, 14.96), (15.69, 18.10))
    source = make_source(tmp_path, seconds=19)
    samples = array("h", [0]) * (19 * 48000)
    for start, _end in cuts:
        onset = round((start + 0.5) * 48000)
        for i in range(1440):
            samples[onset + i] = round(10000 * math.sin(i * 1000 * math.tau / 48000))
    with wave.open(str(tmp_path / "pulses.wav"), "w") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    replacement = tmp_path / "pulses.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "source.mp4"),
                    "-i", str(tmp_path / "pulses.wav"), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", str(replacement)], check=True, capture_output=True)
    replacement.replace(tmp_path / "source.mp4")
    source.sha256 = hashlib.sha256((tmp_path / "source.mp4").read_bytes()).hexdigest()
    source.bytes = (tmp_path / "source.mp4").stat().st_size
    plan, analysis = edit(cuts=cuts)
    output = render_imported_video(source, plan, analysis, tmp_path,
                                   Settings(width=360, height=640, min_seconds=1, instagram_edit_mode="original"), [])
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1",
                              "-ar", "48000", "-f", "s16le", "-"], check=True, capture_output=True)
    rendered = array("h", decoded.stdout)
    for i in range(6):
        begin = round(i * 2.4 * 48000)
        window = rendered[begin:begin + 48000]
        onset = next(index for index, value in enumerate(window) if abs(value) > 1000)
        assert onset / 48000 == pytest.approx(0.5, abs=1 / 30)
