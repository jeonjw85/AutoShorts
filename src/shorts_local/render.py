import math
import os
from fractions import Fraction
from pathlib import Path

from .artwork import scene_art
from .config import Settings
from .media import audio_duration, probe
from .models import Story
from .process import atomic_json, run_process


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02},{millis % 1000:03}"


def validate_video(path: Path, settings: Settings, *, min_seconds: float | None = None,
                   full_decode: bool = False) -> dict:
    data = probe(path)
    videos = [s for s in data["streams"] if s["codec_type"] == "video"]
    audios = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("영상과 음성 스트림이 각각 하나씩 필요합니다.")
    video, audio = videos[0], audios[0]
    duration = float(data["format"]["duration"])
    minimum = settings.min_seconds if min_seconds is None else min_seconds
    durations = [duration, float(video.get("duration", duration)), float(audio.get("duration", duration))]
    if duration < minimum - 0.00001 or max(durations) > settings.max_seconds + 0.00001:
        raise ValueError(f"완성 영상 길이 {duration:.1f}초가 설정 범위를 벗어났습니다.")
    if (video["width"], video["height"]) != (settings.width, settings.height):
        raise ValueError("영상 해상도가 설정과 다릅니다.")
    if video["codec_name"] != "h264" or audio["codec_name"] != "aac":
        raise ValueError("H.264/AAC 코덱이 필요합니다.")
    if Fraction(video["avg_frame_rate"]) != settings.fps:
        raise ValueError("영상 프레임 속도가 설정과 다릅니다.")
    if abs(float(video.get("duration", duration)) - float(audio.get("duration", duration))) > 0.3:
        raise ValueError("영상과 음성 길이가 맞지 않습니다.")
    if full_decode:
        run_process(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                    path.parent / "validate.log", settings.stage_timeout)
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "audio_codec": audio["codec_name"], "video_codec": video["codec_name"]}


def render_video(story: Story, workdir: Path, settings: Settings) -> Path:
    workdir = workdir.resolve()
    durations = [audio_duration(workdir / "voice" / f"{i:03}.wav") for i in range(len(story.scenes))]
    durations = [math.ceil((d + 0.18) * settings.fps) / settings.fps for d in durations]
    if not settings.min_seconds <= sum(durations) <= settings.max_seconds:
        raise ValueError(f"음성 총 길이 {sum(durations):.1f}초가 설정 범위를 벗어났습니다.")
    segments = workdir / "segments"
    segments.mkdir(exist_ok=True)
    captions, timeline, cursor = [], [], 0.0
    for i, (scene, duration) in enumerate(zip(story.scenes, durations, strict=True)):
        bg, overlay = scene_art(story, i, workdir, settings)
        frames = round(duration * settings.fps)
        target = segments / f"{i:03}.mp4"
        filters = (
            f"[0:v]scale={settings.width * 2}:{settings.height * 2},"
            f"zoompan=z='min(1+on*0.00015,1.045)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
            f"d={frames}:s={settings.width}x{settings.height}:fps={settings.fps},setsar=1[bg];"
            "[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v];[2:a]apad[a]"
        )
        run_process([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(bg),
            "-i", str(overlay), "-i", str(workdir / "voice" / f"{i:03}.wav"),
            "-filter_complex", filters, "-map", "[v]", "-map", "[a]",
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "1", str(target),
        ], workdir / "render.log", settings.stage_timeout)
        captions.append(f"{i + 1}\n{srt_time(cursor)} --> {srt_time(cursor + duration)}\n{scene.narration}\n")
        timeline.append({"scene": i, "start": cursor, "end": cursor + duration})
        cursor += duration
    # MP4 edit-list/container durations can round fractional frames (e.g. 10.666016
    # instead of 320/30). Explicit frame-derived durations prevent cumulative gaps.
    (segments / "scenes.txt").write_text("\n".join(
        f"file '{i:03}.mp4'\nduration {duration:.9f}" for i, duration in enumerate(durations)))
    temp, output = workdir / "video.partial.mp4", workdir / "video.mp4"
    run_process([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
        "-i", "scenes.txt", "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", str(temp),
    ], workdir / "render.log", settings.stage_timeout, cwd=segments)
    report = validate_video(temp, settings, full_decode=True)
    os.replace(temp, output)
    (workdir / "captions.srt").write_text("\n".join(captions))
    atomic_json(workdir / "timeline.json", timeline)
    atomic_json(workdir / "validation.json", report)
    return output
