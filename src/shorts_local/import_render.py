"""Frame-based source editing with one common media clock and two audio modes."""

import math
import os
import re
from pathlib import Path

from pydantic import Field

from .artwork import import_overlay
from .config import Settings
from .editing import minimum_duration, validate_edit_plan
from .editing_models import Analysis, EditPlan
from .instagram_models import SourceMedia, ValueModel
from .media import audio_duration, probe
from .process import atomic_json, run_process
from .render import srt_time, validate_video
from .youtube import file_hash


class CaptionCue(ValueModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    text: str = Field(min_length=1)


class TimelineClip(ValueModel):
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    start_frame: int = Field(ge=0)
    frames: int = Field(gt=0)
    speed: float = Field(ge=0.8, le=1.25)
    voice: str | None = None


class Timeline(ValueModel):
    fps: int = Field(gt=0)
    frames: int = Field(gt=0)
    clips: list[TimelineClip] = Field(min_length=1, max_length=8)
    captions: list[CaptionCue]


class NarrationTimingError(ValueError):
    def __init__(self, durations: list[float]):
        self.durations = durations
        super().__init__("해설 길이에 맞춰 클립 또는 문장을 한 번 조정해야 합니다.")


def build_timeline(plan: EditPlan, source: SourceMedia, analysis: Analysis, settings: Settings,
                   voices: list[Path], *, workdir: Path) -> Timeline:
    validate_edit_plan(plan, source, analysis, settings)
    narrated = plan.mode == "narrated"
    if len(voices) != (len(plan.clips) if narrated else 0):
        raise ValueError("클립 수와 해설 음성 수가 다릅니다.")
    durations = [audio_duration(path) for path in voices]
    clips, captions, cursor = [], [], 0
    translated = {t.segment_id: t.text for t in plan.translations}
    for index, clip in enumerate(plan.clips):
        length = clip.end - clip.start
        frames = (math.ceil(durations[index] * settings.fps - 1e-7) if narrated
                  else math.floor(length * settings.fps + 1e-7))
        speed = length / (frames / settings.fps) if narrated and frames else 1.0
        if narrated and not 0.8 - 1e-7 <= speed <= 1.25 + 1e-7:
            raise NarrationTimingError(durations)
        if frames < 1:
            raise ValueError("선택한 클립이 한 프레임보다 짧습니다.")
        voice = str(voices[index].resolve().relative_to(workdir.resolve())) if narrated else None
        clips.append(TimelineClip(source_start=clip.start, source_end=clip.end, start_frame=cursor,
                                  frames=frames, speed=min(1.25, max(0.8, speed)), voice=voice))
        if narrated:
            captions.append(CaptionCue(start_frame=cursor, end_frame=cursor + frames, text=clip.narration))
        else:
            for segment in analysis.transcript.segments:
                if segment.start < clip.end and segment.end > clip.start:
                    start = cursor + max(0, round((segment.start - clip.start) * settings.fps))
                    end = cursor + min(frames, round((segment.end - clip.start) * settings.fps))
                    if end > start:
                        captions.append(CaptionCue(start_frame=start, end_frame=end, text=translated[segment.id]))
        cursor += frames
    if not minimum_duration(source, settings) <= cursor / settings.fps <= settings.max_seconds:
        if narrated:
            raise NarrationTimingError(durations)
        raise ValueError("선택한 원본 클립의 총 길이가 설정 범위를 벗어났습니다.")
    return Timeline(fps=settings.fps, frames=cursor, clips=clips, captions=captions)


def render_imported_video(source: SourceMedia, plan: EditPlan, analysis: Analysis, workdir: Path,
                           settings: Settings, voices: list[Path]) -> Path:
    workdir = workdir.resolve()
    timeline = build_timeline(plan, source, analysis, settings, voices, workdir=workdir)
    original = workdir / "source.mp4"
    if file_hash(original) != source.sha256:
        raise ValueError("원본 영상의 해시가 변경되었습니다.")
    info = probe(original)
    origin = float(info["format"].get("start_time", 0))
    segments = workdir / "import-segments"
    segments.mkdir(exist_ok=True)
    header = import_overlay(plan, None, workdir / "art/import-header.png", settings)
    w, h, fps = settings.width, settings.height, settings.fps
    fit_height, fit_top = int(h * 0.55) // 2 * 2, round(h * 0.2)
    for index, clip in enumerate(timeline.clips):
        duration = clip.frames / fps
        args = ["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(original), "-i", str(header)]
        filters = [
            f"[0:v]setpts=PTS-({origin})/TB,trim=start={clip.source_start}:end={clip.source_end},"
            f"setpts=(PTS-({clip.source_start})/TB)/{clip.speed},fps={fps}:start_time=0,"
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,split[back][front]",
            f"[back]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=20[bg]",
            f"[front]scale={w}:{fit_height}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg]",
            f"[bg][fg]overlay=(W-w)/2:{fit_top}+({fit_height}-h)/2[fit]",
            "[fit][1:v]overlay=0:0[v0]",
        ]
        current, input_count = "v0", 2
        for number, cue in enumerate(timeline.captions):
            if cue.start_frame < clip.start_frame or cue.start_frame >= clip.start_frame + clip.frames:
                continue
            path = import_overlay(plan, cue.text, workdir / f"art/import-caption-{number:03}.png", settings)
            args += ["-i", str(path)]
            start, end = cue.start_frame - clip.start_frame, cue.end_frame - clip.start_frame
            filters += [f"[{input_count}:v]crop=iw:ih/4:0:ih*3/4[c{number}]",
                        f"[{current}][c{number}]overlay=0:H-h:enable='gte(n,{start})*lt(n,{end})'[v{number + 1}]"]
            current, input_count = f"v{number + 1}", input_count + 1
        if clip.voice:
            args += ["-i", str(workdir / clip.voice)]
            audio = f"[{input_count}:a]asetpts=PTS-STARTPTS,apad,atrim=duration={duration}"
        elif source.has_audio:
            audio = (f"[0:a]asetpts=PTS-({origin})/TB,aresample=async=1:first_pts=0,"
                     f"atrim=start={clip.source_start}:end={clip.source_end},"
                     f"asetpts=PTS-({clip.source_start})/TB,apad,atrim=duration={duration}")
        else:
            audio = f"anullsrc=r=48000:cl=mono,atrim=duration={duration}"
        filters += [audio + ",aresample=48000[a]", f"[{current}]setsar=1,format=yuv420p[v]"]
        run_process([*args, "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                     "-map", "[v]", "-map", "[a]", "-t", str(duration), "-c:v", "libx264",
                     "-preset", "fast", "-crf", "20", "-threads", "2", "-c:a", "pcm_s16le", "-ar", "48000",
                     "-ac", "1", "-movie_timescale", "30000", str(segments / f"{index:03}.mov")],
                    workdir / "render.log", settings.stage_timeout)
    (segments / "clips.txt").write_text("\n".join(
        f"file '{i:03}.mov'\nduration {clip.frames / fps:.9f}" for i, clip in enumerate(timeline.clips)))
    temp, output = workdir / "video.partial.mp4", workdir / "video.mp4"
    duration = timeline.frames / fps
    # PCM intermediates avoid accumulating AAC priming/padding at each cut.
    # Normalize audible selections only; silent inputs must remain silent.
    level_log = workdir / "audio-level.log"
    level_log.unlink(missing_ok=True)
    run_process(["ffmpeg", "-v", "info", "-f", "concat", "-safe", "1", "-i", "clips.txt",
                 "-vn", "-af", "volumedetect", "-f", "null", "-"],
                level_log, settings.stage_timeout, cwd=segments)
    match = re.search(r"max_volume: ([-\d.]+) dB", level_log.read_text())
    normalize = "loudnorm=I=-16:TP=-1.5:LRA=11," if match and float(match[1]) > -80 else ""
    run_process(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", "clips.txt",
                 "-c:v", "copy", "-af", f"{normalize}aresample=48000,apad,"
                 f"atrim=end_sample={round(duration * 48000)},asetpts=N/SR/TB",
                 "-c:a", "aac", "-ar", "48000", "-t", str(duration), "-movie_timescale", "30000",
                 "-movflags", "+faststart", str(temp)],
                workdir / "render.log", settings.stage_timeout, cwd=segments)
    report = validate_video(temp, settings, min_seconds=minimum_duration(source, settings), full_decode=True)
    report.update(sha256=file_hash(temp), min_seconds=minimum_duration(source, settings))
    run_process(["ffmpeg", "-v", "error", "-y", "-i", str(temp), "-frames:v", "1", str(workdir / "cover.partial.png")],
                workdir / "render.log", settings.stage_timeout)
    captions = "\n".join(f"{i + 1}\n{srt_time(c.start_frame / fps)} --> {srt_time(c.end_frame / fps)}\n{c.text}\n"
                         for i, c in enumerate(timeline.captions))
    (workdir / "captions.partial.srt").write_text(captions)
    atomic_json(workdir / "timeline.json", timeline.model_dump())
    atomic_json(workdir / "validation.json", report)
    os.replace(temp, output)
    os.replace(workdir / "captions.partial.srt", workdir / "captions.srt")
    os.replace(workdir / "cover.partial.png", workdir / "cover.png")
    return output
