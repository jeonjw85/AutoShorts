"""Frame/audio preparation and validated checkpoints; model weights live in children."""

import importlib.util
import json
import math
import re
import sys
import wave
from array import array
from bisect import bisect_right
from pathlib import Path

from .artifacts import artifact_key, read_artifact, write_artifact
from .config import Settings
from .editing_models import Analysis, Observation, Transcript, TranscriptSegment
from .instagram_models import SourceMedia
from .media import probe
from .process import BlockedError, atomic_json, run_process
from .youtube import file_hash


def normalize_transcript(raw: dict, duration: float) -> Transcript:
    segments = []
    for index, row in enumerate(raw["segments"]):
        if row.get("no_speech_prob", 0) > 0.6 and row.get("avg_logprob", 0) < -1.0:
            continue
        text = row["text"].strip()
        if not text:
            continue
        segment = TranscriptSegment(id=f"s{index:03}", start=row["start"], end=row["end"], text=text)
        if segment.end > duration:
            raise ValueError("발화 시간이 원본 길이를 벗어났습니다.")
        previous = 0.0
        words = row.get("words", [])
        for word in words:
            start, end = float(word["start"]), float(word["end"])
            if not (math.isfinite(start) and math.isfinite(end)
                    and previous <= start <= end <= duration):
                raise ValueError("단어의 발화 시간이 올바르지 않습니다.")
            previous = end
        if words:
            # Whisper's forced word alignment may extend the coarse segment
            # window. Preserve that more precise speech boundary for cutting.
            segment = TranscriptSegment(id=segment.id, start=words[0]["start"],
                                        end=words[-1]["end"], text=text)
        segments.append(segment)
    return Transcript(language=raw.get("language"), segments=segments)


def _has_signal(path: Path) -> bool:
    with wave.open(str(path), "rb") as audio:
        while chunk := audio.readframes(16000):
            samples = array("h", chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            if any(abs(sample) > 8 for sample in samples):
                return True
    return False


def _extract_audio(source: SourceMedia, workdir: Path, settings: Settings) -> Path:
    info = probe(workdir / "source.mp4")
    origin = float(info["format"].get("start_time", 0))
    path = workdir / "source-audio.wav"
    run_process(["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(workdir / "source.mp4"),
                 "-map", "0:a:0", "-vn", "-af", f"asetpts=PTS-({origin})/TB,aresample=async=1:first_pts=0",
                 "-t", str(source.duration), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)],
                workdir / "analyze.log", settings.stage_timeout)
    return path


def sample_frames(source: SourceMedia, workdir: Path, settings: Settings) -> list[tuple[float, Path]]:
    workdir = workdir.resolve()
    folder = workdir / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    scene_file = workdir / "scene-times.txt"
    scene_file.unlink(missing_ok=True)
    run_process(["ffmpeg", "-v", "error", "-y", "-i", str(workdir / "source.mp4"),
                 "-an", "-vf", "scale=160:-2,select='gt(scene,0.3)',metadata=print:file=scene-times.txt",
                 "-f", "null", "-"], workdir / "analyze.log", settings.stage_timeout, cwd=workdir)
    count = min(24, max(2, math.ceil(source.duration)))
    last = max(0, source.duration - 1 / settings.fps)
    times = {round(index * last / (count - 1), 6) for index in range(count)}
    if scene_file.exists():
        changes = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", scene_file.read_text())]
        for when in changes:
            if len(times) >= 48:
                break
            if 0 <= when <= last:
                times.add(round(when, 6))
    # Container/audio duration need not end on a video frame. Snap requests to
    # actual decoded presentation timestamps, including variable-frame-rate input.
    clock_file = workdir / "frame-clock.json"
    run_process(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "frame=best_effort_timestamp_time", "-of", "json", "-o", str(clock_file),
                 str(workdir / "source.mp4")], workdir / "analyze.log", settings.stage_timeout)
    origin = float(probe(workdir / "source.mp4")["format"].get("start_time", 0))
    clock = sorted(float(f["best_effort_timestamp_time"]) - origin
                   for f in json.loads(clock_file.read_text())["frames"])
    if not clock:
        raise ValueError("원본 영상에서 프레임을 찾을 수 없습니다.")
    times = {max(0, clock[max(0, bisect_right(clock, when) - 1)]) for when in times}
    frames = []
    for index, when in enumerate(sorted(times)):
        path = folder / f"{index:03}.png"
        run_process(["ffmpeg", "-v", "error", "-y", "-ss", str(max(0, when - 0.00001)), "-i", str(workdir / "source.mp4"),
                     "-frames:v", "1", "-vf",
                     "scale=w='min(768,iw)':h='min(768,ih)':force_original_aspect_ratio=decrease",
                     str(path)], workdir / "analyze.log", settings.stage_timeout)
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            if min(image.size) < 1 or max(image.size) > 768:
                raise ValueError("관찰용 프레임 크기가 올바르지 않습니다.")
        frames.append((when, path))
    atomic_json(workdir / "frames.json", [{"time": t, "path": str(p.relative_to(workdir))} for t, p in frames])
    return frames


def _engine(stage, workdir, settings):
    package = "mlx_whisper" if stage == "transcribe" else "mlx_vlm"
    if importlib.util.find_spec(package) is None:
        raise BlockedError("분석 엔진 설치가 필요합니다: uv sync --extra mlx --extra images --extra instagram")
    run_process([sys.executable, "-m", "shorts_local.analysis_engines", stage, str(workdir)],
                workdir / f"{stage}.log", settings.stage_timeout)


def analyze_source(source: SourceMedia, workdir: Path, settings: Settings) -> Analysis:
    if file_hash(workdir / "source.mp4") != source.sha256:
        raise ValueError("원본 영상의 해시가 변경되었습니다.")
    settings.save(workdir / "settings.json")
    transcript_key = artifact_key("transcribe", {"source": source.sha256, "model": settings.transcription_model,
                                                "alignment": 2})
    saved = read_artifact(workdir / "transcript.json", transcript_key)
    try:
        transcript = Transcript.model_validate(saved) if saved is not None else None
    except ValueError:
        transcript = None
    if transcript is None:
        if not source.has_audio or not _has_signal(_extract_audio(source, workdir, settings)):
            transcript = Transcript()
        else:
            (workdir / "transcript.partial.json").unlink(missing_ok=True)
            _engine("transcribe", workdir, settings)
            transcript = normalize_transcript(json.loads((workdir / "transcript.partial.json").read_text()), source.duration)
        write_artifact(workdir / "transcript.json", transcript_key, transcript.model_dump())
    if any(s.end > source.duration for s in transcript.segments):
        raise ValueError("저장된 발화가 원본 길이를 벗어났습니다.")
    observation_key = artifact_key("observe", {"source": source.sha256, "model": settings.vision_model,
                                               "sampler": 2, "fps": settings.fps})
    saved = read_artifact(workdir / "observations.json", observation_key)
    observations = None
    if saved is not None:
        try:
            observations = Analysis(transcript=transcript, observations=saved["observations"]).observations
        except (ValueError, KeyError, TypeError):
            pass
    if observations is None:
        frames = sample_frames(source, workdir, settings)
        (workdir / "observations.partial.json").unlink(missing_ok=True)
        _engine("observe", workdir, settings)
        raw = json.loads((workdir / "observations.partial.json").read_text())["observations"]
        if len(raw) != len(frames):
            raise ValueError("프레임 관찰 개수가 일치하지 않습니다.")
        observations = [Observation(id=f"v{i:03}", time=when, text=row["text"])
                        for i, ((when, _), row) in enumerate(zip(frames, raw, strict=True))]
        write_artifact(workdir / "observations.json", observation_key,
                       {"observations": [o.model_dump() for o in observations]})
    if any(o.time >= source.duration for o in observations):
        raise ValueError("관찰 시간이 원본 길이를 벗어났습니다.")
    return Analysis(transcript=transcript, observations=observations)
