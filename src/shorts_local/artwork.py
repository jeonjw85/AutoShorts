import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .models import Scene, Story, VideoMetadata
from .process import BlockedError

BASE = (1080, 1920)
INK = (12, 22, 34)
CREAM = (245, 238, 222)
ACCENT = (232, 178, 87)


def font(size: int, path: str | None = None):
    candidates = [path] if path else [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size, index=0)
    raise BlockedError("한글 폰트가 필요합니다. shorts-local config --channel <채널ID> font_path <경로>를 지정하세요.")


def wrap_text(text: str, face, width: int) -> list[str]:
    result = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if face.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                result.append(line)
                line = ""
            # Prefer word boundaries; only split a word that cannot fit on its own.
            for char in word:
                if line and face.getlength(line + char) > width:
                    result.append(line)
                    line = ""
                line += char
        result.append(line)
    return result


def draw_fitted(draw, text, xy, width, height, size, settings, color=CREAM):
    for current in range(size, 25, -2):
        face = font(current, settings.font_path)
        lines = wrap_text(text, face, width)
        line_height = int(current * 1.35)
        if len(lines) * line_height <= height:
            for i, line in enumerate(lines):
                draw.text((xy[0], xy[1] + i * line_height), line, font=face, fill=color)
            return
    raise ValueError("자막이 화면 안전 영역에 들어가지 않습니다.")


def illustrated_background(scene: Scene, index: int) -> Image.Image:
    image = Image.new("RGB", BASE, INK)
    draw = ImageDraw.Draw(image)
    seed = int(hashlib.sha256(scene.image_prompt.encode()).hexdigest()[:8], 16)
    for y in range(BASE[1]):
        t = y / BASE[1]
        draw.line([(0, y), (1080, y)], fill=(int(12 + 17 * t), int(22 + 27 * t), int(34 + 28 * t)))
    # Distinct, deterministic editorial illustrations; no downloaded assets or invented credits.
    for i in range(50):
        x, y = (seed * (i + 1) % 1000) + 40, (seed // (i + 1) % 1100) + 340
        radius = 1 + i % 3
        draw.ellipse((x, y, x + radius, y + radius), fill=(85, 103, 111))
    draw.ellipse((105, 470, 975, 1340), fill=(36, 65, 76), outline=(66, 92, 98), width=2)
    draw.ellipse((170, 535, 910, 1275), outline=(65, 87, 92), width=2)
    text = scene.narration + scene.heading
    if any(word in text for word in ("씨앗", "꽃", "화분", "나무", "심었")):
        draw.polygon([(365, 990), (715, 990), (667, 1200), (413, 1200)], fill=(183, 113, 75))
        draw.rounded_rectangle((347, 965, 733, 1020), 20, fill=(220, 149, 102))
        draw.line((540, 980, 540, 740), fill=(159, 191, 151), width=14)
        draw.ellipse((400, 735, 549, 823), fill=(129, 173, 145))
        draw.ellipse((542, 670, 699, 770), fill=(168, 198, 155))
    elif any(word in text for word in ("편지", "봉투", "우체", "이름")):
        draw.rounded_rectangle((270, 760, 810, 1110), 28, fill=CREAM)
        draw.polygon([(280, 775), (540, 973), (800, 775)], fill=(222, 207, 181))
        draw.line([(285, 1090), (460, 945)], fill=(178, 165, 144), width=3)
        draw.line([(795, 1090), (620, 945)], fill=(178, 165, 144), width=3)
        draw.ellipse((503, 938, 577, 1012), fill=ACCENT)
    else:
        draw.ellipse((365, 635, 715, 985), fill=ACCENT)
        for i in range(8):
            x = 230 + i * 82
            top = 900 + int(math.sin(i + index) * 110)
            draw.rounded_rectangle((x, top, x + 65, 1190), 8, fill=(18 + i * 3, 35 + i * 3, 46 + i * 2))
            for y in range(top + 20, 1160, 45):
                draw.rectangle((x + 18, y, x + 30, y + 18), fill=(195, 161, 111))
    return image


def scene_art(story: Story, index: int, workdir: Path, settings: Settings) -> tuple[Path, Path]:
    scene = story.scenes[index]
    assets = workdir / "art"
    assets.mkdir(exist_ok=True)
    generated = workdir / "images" / f"{index:03}.png"
    if settings.visuals == "mflux":
        with Image.open(generated) as original:
            background = ImageOps.fit(original.convert("RGB"), BASE)
    else:
        background = illustrated_background(scene, index)

    overlay = Image.new("RGBA", BASE)
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 0, 1080, 440), fill=(*INK, 220))
    draw.rectangle((0, 1280, 1080, 1920), fill=(*INK, 230))
    draw.rounded_rectangle((86, 116, 100, 159), 6, fill=ACCENT)
    draw.text((120, 115), settings.channel_name, font=font(34, settings.font_path), fill=CREAM)
    draw.text((84, 194), f"STORY  /  {index + 1:02} — {len(story.scenes):02}",
              font=font(25, settings.font_path), fill=ACCENT)
    draw_fitted(draw, scene.heading, (80, 260), 845, 165, 78, settings)
    draw_fitted(draw, scene.narration, (84, 1340), 810, 345, 61, settings)
    draw.line((86, 1750, 904, 1750), fill=(85, 99, 105), width=4)
    draw.line((86, 1750, 86 + int(818 * (index + 1) / len(story.scenes)), 1750),
              fill=ACCENT, width=6)
    draw.text((84, 1790), "한 편의 창작 이야기", font=font(26, settings.font_path), fill=(160, 174, 179))
    target_size = (settings.width, settings.height)
    background = background.resize(target_size, Image.Resampling.LANCZOS)
    overlay = overlay.resize(target_size, Image.Resampling.LANCZOS)
    bg_path, overlay_path = assets / f"{index:03}-bg.png", assets / f"{index:03}-overlay.png"
    background.save(bg_path)
    overlay.save(overlay_path)
    if index == 0:
        Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB").save(workdir / "cover.png")
    return bg_path, overlay_path


def import_overlay(metadata: VideoMetadata, caption: str | None, path: Path, settings: Settings) -> Path:
    image = Image.new("RGBA", BASE)
    draw = ImageDraw.Draw(image)
    color = tuple(bytes.fromhex(settings.instagram_accent_color[1:]))
    draw.rectangle((0, 0, 1080, 345), fill=(*INK, 215))
    draw.rounded_rectangle((76, 100, 91, 139), 6, fill=color)
    draw.text((114, 98), settings.channel_name, font=font(34, settings.font_path), fill=CREAM)
    draw_fitted(draw, metadata.title, (76, 180), 920, 150, 66, settings)
    if caption:
        draw.rounded_rectangle((48, 1490, 1032, 1840), 28, fill=(*INK, 235))
        draw_fitted(draw, caption, (84, 1530), 900, 270, 58, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize((settings.width, settings.height), Image.Resampling.LANCZOS).save(path)
    return path
