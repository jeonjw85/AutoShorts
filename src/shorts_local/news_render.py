"""Large, two-line news cards on a frame-aligned narration timeline."""

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .artwork import BASE, CREAM, INK, draw_fitted, font
from .config import Settings
from .media import audio_duration
from .models import VideoMetadata
from .news_fetch import ArticleDocument
from .news_plan import NewsPlan, display_date, validate_news_plan
from .process import atomic_json, run_process
from .render import srt_time, validate_video


def _line(draw, text, y, settings, *, size=96, color=CREAM, centered=True):
    for current in range(size, 25, -2):
        face = font(current, settings.font_path)
        width = draw.textlength(text, font=face)
        if width <= 912:
            x = (1080 - width) / 2 if centered else 84
            draw.text((x, y), text, font=face, fill=color)
            return
    raise ValueError("뉴스 카드 문구가 화면 안전 영역을 벗어납니다.")


def _card(plan, article, index, settings, image):
    card = plan.cards[index]
    canvas = Image.new("RGB", BASE, INK)
    accent = settings.instagram_accent_color
    if image is not None:
        with Image.open(image) as photo:
            canvas.paste(ImageOps.fit(photo.convert("RGB"), (1080, 580)), (0, 200))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((84, 95, 105, 134), 7, fill=accent)
    _line(draw, settings.channel_name, 88, settings, size=44)
    _line(draw, f"NEWS  ·  {index + 1} / {len(plan.cards)}", 157, settings, size=28, color=accent)
    if image is None:
        draw.rounded_rectangle((84, 312, 996, 682), 30, outline=accent, width=3)
        _line(draw, "오늘의 소식", 454, settings, size=60, color=accent)
    if card.highlight:
        _line(draw, card.highlight, 796, settings, size=128, color=accent)
    for row, line in enumerate(card.lines):
        _line(draw, line, 960 + row * 140, settings, color=CREAM if row == 0 else accent)
    draw_fitted(draw, card.narration, (84, 1260), 912, 150, 36, settings, color="#BEC8D2")
    step = 912 / len(plan.cards)
    for i in range(len(plan.cards)):
        draw.rounded_rectangle((84 + i * step, 1440, 84 + (i + 1) * step - 14, 1450), 4,
                               fill=accent if i <= index else "#34404C")
    _line(draw, f"출처  {article.site_name}", 1488, settings, size=32, centered=False, color="#BEC8D2")
    _line(draw, display_date(article, settings), 1540, settings, size=32, centered=False, color="#BEC8D2")
    return canvas.resize((settings.width, settings.height), Image.Resampling.LANCZOS)


def render_news_video(plan: NewsPlan, article: ArticleDocument, voices: list[Path],
                      workdir: Path, settings: Settings, image: Path | None = None) -> Path:
    plan = validate_news_plan(plan, article, settings)
    if len(voices) != len(plan.cards):
        raise ValueError("뉴스 카드 수와 음성 수가 다릅니다.")
    workdir = workdir.resolve()
    durations = [math.ceil((audio_duration(path) + 0.18) * settings.fps) / settings.fps for path in voices]
    total = sum(durations)
    if not settings.min_seconds <= total <= settings.max_seconds:
        raise ValueError(f"뉴스 내레이션 총 길이 {total:.1f}초가 설정 범위를 벗어났습니다.")
    segments = workdir / "news-segments"
    segments.mkdir(parents=True, exist_ok=True)
    captions, timeline, cursor = [], [], 0
    for i, (card, voice, duration) in enumerate(zip(plan.cards, voices, durations, strict=True)):
        still = segments / f"{i:03}.png"
        _card(plan, article, i, settings, image).save(still)
        frames = round(duration * settings.fps)
        run_process([
            "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(settings.fps),
            "-i", str(still), "-i", str(voice.resolve()), "-map", "0:v", "-map", "1:a",
            "-vf", "setsar=1,format=yuv420p", "-af", f"apad,atrim=duration={duration},aresample=48000",
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", "-movie_timescale", "30000",
            str(segments / f"{i:03}.mov"),
        ], workdir / "render.log", settings.stage_timeout)
        start, end = cursor / settings.fps, (cursor + frames) / settings.fps
        captions.append(f"{i + 1}\n{srt_time(start)} --> {srt_time(end)}\n{card.narration}\n")
        timeline.append({"card": i, "start_frame": cursor, "frames": frames,
                         "lines": card.lines, "narration": card.narration,
                         "source": article.site_name, "date": display_date(article, settings)})
        cursor += frames
    (segments / "cards.txt").write_text("\n".join(
        f"file '{i:03}.mov'\nduration {duration:.9f}" for i, duration in enumerate(durations)))
    temp, video = workdir / "video.partial.mp4", workdir / "video.mp4"
    run_process([
        "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", "cards.txt",
        "-c:v", "copy", "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,apad,atrim=duration={total}",
        "-c:a", "aac", "-ar", "48000", "-t", str(total), "-movie_timescale", "30000",
        "-movflags", "+faststart", str(temp),
    ], workdir / "render.log", settings.stage_timeout, cwd=segments)
    validate_video(temp, settings, full_decode=True)
    run_process(["ffmpeg", "-v", "error", "-y", "-i", str(temp), "-frames:v", "1", str(workdir / "cover.png")],
                 workdir / "render.log", settings.stage_timeout)
    (workdir / "captions.srt").write_text("\n".join(captions))
    atomic_json(workdir / "timeline.json", {"fps": settings.fps, "frames": cursor, "cards": timeline})
    atomic_json(workdir / "metadata.json", VideoMetadata(title=plan.title, description=plan.description).model_dump())
    os.replace(temp, video)
    return video
