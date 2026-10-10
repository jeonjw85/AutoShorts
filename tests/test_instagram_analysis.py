import json
import sys

import pytest
from PIL import Image

from shorts_local.config import Settings
from shorts_local.process import atomic_json
from tests.instagram_helpers import make_source


def fake_models(monkeypatch, target, *, fail_observation=False):
    original = target.run_process
    calls = []

    def run(args, log, timeout=1800, **kwargs):
        if args[:3] != [sys.executable, "-m", "shorts_local.analysis_engines"]:
            return original(args, log, timeout, **kwargs)
        stage, folder = args[3], log.parent
        calls.append(stage)
        if stage == "transcribe":
            atomic_json(folder / "transcript.partial.json", {"language": "en", "segments": [
                {"start": 0.2, "end": 1.2, "text": "A test sound", "no_speech_prob": 0.1,
                 "avg_logprob": -0.2, "words": [{"word": "sound", "start": 0.2, "end": 1.2}]},
            ]})
        else:
            if fail_observation:
                raise RuntimeError("model interrupted")
            frames = json.loads((folder / "frames.json").read_text())
            atomic_json(folder / "observations.partial.json", {"observations": [
                {"id": f"v{i:03}", "time": f["time"], "text": "A moving test pattern."}
                for i, f in enumerate(frames)
            ]})

    monkeypatch.setattr(target, "run_process", run)
    return calls


@pytest.mark.parametrize("audio,silent", [(False, False), (True, True)])
def test_no_audio_and_silence_produce_empty_transcript(tmp_path, monkeypatch, audio, silent):
    import shorts_local.analysis as analysis

    source = make_source(tmp_path, audio=audio, silent=silent)
    calls = fake_models(monkeypatch, analysis)
    result = analysis.analyze_source(source, tmp_path, Settings())
    assert result.transcript.segments == []
    assert calls == ["observe"]
    assert result.observations
    empty = analysis.normalize_transcript({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "hallucination", "no_speech_prob": 0.9, "avg_logprob": -2},
    ]}, source.duration)
    assert empty.segments == []


def test_transcript_timestamps_and_frame_bounds(tmp_path):
    import shorts_local.analysis as analysis
    from shorts_local.editing_models import TranscriptSegment

    source = make_source(tmp_path, width=960, height=540)
    frames = analysis.sample_frames(source, tmp_path, Settings())
    assert 2 <= len(frames) <= 48
    assert frames[0][0] == 0 and frames[-1][0] > 1.8
    for when, path in frames:
        assert 0 <= when < source.duration
        with Image.open(path) as image:
            assert max(image.size) <= 768
    for start, end in ((-1, 1), (0, float("nan")), (1, 1), (2, 1), (0, float("inf"))):
        with pytest.raises(ValueError):
            TranscriptSegment(id="s0", start=start, end=end, text="speech")
    with pytest.raises(ValueError):
        analysis.normalize_transcript({"language": "en", "segments": [
            {"start": 0, "end": 3, "text": "outside source"},
        ]}, 2)
    with pytest.raises(ValueError):
        analysis.normalize_transcript({"language": "en", "segments": [
            {"start": 0, "end": 1, "text": "speech",
             "words": [{"start": 0.8, "end": 0.5, "word": "bad"}]},
        ]}, 2)


def test_analysis_checkpoints_are_bound_to_source_and_models(tmp_path, monkeypatch):
    import shorts_local.analysis as analysis

    source = make_source(tmp_path)
    calls = fake_models(monkeypatch, analysis, fail_observation=True)
    with pytest.raises(RuntimeError, match="interrupted"):
        analysis.analyze_source(source, tmp_path, Settings())
    assert calls == ["transcribe", "observe"]
    monkeypatch.undo()
    calls = fake_models(monkeypatch, analysis)
    first = analysis.analyze_source(source, tmp_path, Settings())
    assert calls == ["observe"]
    assert first.transcript.segments[0].start == 0.2
    analysis.analyze_source(source, tmp_path, Settings())
    assert calls == ["observe"]
    analysis.analyze_source(source, tmp_path, Settings(vision_model="different-local-model"))
    assert calls == ["observe", "observe"]
    (tmp_path / "transcript.json").write_text("{")
    analysis.analyze_source(source, tmp_path, Settings(vision_model="different-local-model"))
    assert calls[-1] == "transcribe"
    changed = make_source(tmp_path, seconds=3)
    analysis.analyze_source(changed, tmp_path, Settings())
    assert calls[-2:] == ["transcribe", "observe"]


def test_vlm_batches_keep_frame_identity(tmp_path, monkeypatch):
    import shorts_local.analysis_engines as engines

    frames = [{"time": i / 2, "path": f"frames/{i}.png"} for i in range(9)]
    for frame in frames:
        path = tmp_path / frame["path"]
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (64, 64)).save(path)
    atomic_json(tmp_path / "frames.json", frames)
    calls = []
    monkeypatch.setattr(engines, "describe_images", lambda settings, paths: (
        calls.append(len(paths)) or ["An observed frame." for _ in paths]))
    engines.observe(Settings(), tmp_path)
    rows = json.loads((tmp_path / "observations.partial.json").read_text())["observations"]
    assert calls == [4, 4, 1]
    assert [r["time"] for r in rows] == [i / 2 for i in range(9)]
    assert len({r["id"] for r in rows}) == 9


def test_aligned_words_can_extend_whispers_coarse_segment_window():
    from shorts_local.analysis import normalize_transcript

    result = normalize_transcript({"language": "ko", "segments": [
        {"start": 1.5, "end": 3.0, "text": "그 멜로디",
         "words": [{"word": "그", "start": 1.0, "end": 1.6},
                   {"word": "멜로디", "start": 1.6, "end": 3.1}]},
    ]}, 4)
    assert (result.segments[0].start, result.segments[0].end) == (1.0, 3.1)


def test_sampling_uses_real_video_frames_when_audio_ends_later(tmp_path):
    import shorts_local.analysis as analysis

    source = make_source(tmp_path, seconds=2)
    # AAC container end can be slightly later than the last video frame.
    source.duration = 2.013333
    frames = analysis.sample_frames(source, tmp_path, Settings())
    assert frames[-1][0] <= 1.966667 and frames[-1][1].is_file()


def test_visual_generation_bounds_json_before_token_exhaustion(tmp_path, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    structured = ModuleType("mlx_vlm.structured")
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.structured", structured)
    import shorts_local.analysis_engines as engines

    monkeypatch.setattr(engines, "_vision_model", lambda _: (
        SimpleNamespace(config={}), SimpleNamespace(tokenizer="fixture")))
    prompt_utils.apply_chat_template = lambda *args, **kwargs: "images"

    def constraint(tokenizer, schema):
        assert schema["minItems"] == schema["maxItems"] == 2
        assert schema["items"]["maxLength"] == 200
        return "bounded-json"

    structured.build_json_schema_logits_processor = constraint

    def generate(*args, **kwargs):
        if kwargs.get("logits_processors") == ["bounded-json"]:
            return SimpleNamespace(text='["Clock", "Woman"]')
        return SimpleNamespace(text='["Clock", "A woman ' + "visible " * 1000)

    mlx_vlm.generate = generate
    assert engines.describe_images(Settings(), [tmp_path / "a.png", tmp_path / "b.png"]) == ["Clock", "Woman"]
