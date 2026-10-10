"""Heavy imports are deliberately confined to short-lived GPU worker processes."""

import argparse
import json
import os
from pathlib import Path

from .config import Settings
from .media import audio_duration
from .models import Story


def valid_wav(path: Path) -> bool:
    try:
        audio_duration(path)
        return True
    except ValueError:
        return False


def speech_complete(story: Story, workdir: Path) -> bool:
    return all(valid_wav(workdir / "voice" / f"{i:03}.wav") for i in range(len(story.scenes)))


def valid_image(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.load()
            return image.width > 0 and image.height > 0
    except (OSError, ValueError):
        return False


def images_complete(story: Story, workdir: Path) -> bool:
    return all(valid_image(workdir / "images" / f"{i:03}.png") for i in range(len(story.scenes)))


def generate_plan(settings: Settings, workdir: Path):
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler

    model, tokenizer = load(settings.text_model)
    request = json.loads((workdir / "request.json").read_text())
    prompt = request["prompt"]
    messages = [{"role": "user", "content": prompt}]
    tokens = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False,
                                          enable_thinking=False)
    text = generate(model, tokenizer, prompt=tokens, max_tokens=request.get("max_tokens", 2500),
                    sampler=make_sampler(temp=0.65), verbose=False)
    (workdir / "draft.txt").write_text(text)


def generate_speech(settings: Settings, workdir: Path):
    story = Story.model_validate_json((workdir / "story.json").read_text())
    synthesize_texts(settings, [scene.narration for scene in story.scenes], workdir / "voice")


def generate_edit_speech(settings: Settings, workdir: Path):
    request = json.loads((workdir / "speech-request.json").read_text())
    directory = (workdir / request["voice_subdir"]).resolve()
    if not directory.is_relative_to((workdir / "voice").resolve()):
        raise ValueError("음성 출력 경로가 작업 폴더를 벗어났습니다.")
    synthesize_texts(settings, request["texts"], directory)


def synthesize_texts(settings: Settings, texts: list[str], voice_dir: Path) -> None:
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    voice_dir.mkdir(parents=True, exist_ok=True)
    model = load_model(settings.speech_model)
    for index, text in enumerate(texts):
        path = voice_dir / f"{index:03}.wav"
        if valid_wav(path):
            continue
        chunks, sample_rate = [], None
        for result in model.generate_custom_voice(
            text=text, speaker=settings.speech_voice,
            language="Korean", max_tokens=512,
        ):
            chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
            sample_rate = result.sample_rate
        if not chunks or sample_rate is None:
            raise ValueError("음성 모델이 빈 결과를 반환했습니다.")
        samples = np.concatenate(chunks)
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 0.0001:
            raise ValueError("음성이 무음이거나 올바르지 않습니다.")
        samples = samples / max(1.0, float(np.max(np.abs(samples))) / 0.95)
        temp = path.with_suffix(".partial.wav")
        sf.write(temp, samples, sample_rate, subtype="PCM_16")
        if not valid_wav(temp):
            raise ValueError("생성 음성이 허용된 길이(0.3~40초)를 벗어났습니다.")
        os.replace(temp, path)
        print(f"scene {index + 1}: {len(samples) / sample_rate:.2f}s", flush=True)


def generate_images(settings: Settings, workdir: Path):
    from mflux.models.z_image import ZImageTurbo

    story = Story.model_validate_json((workdir / "story.json").read_text())
    image_dir = workdir / "images"
    image_dir.mkdir(exist_ok=True)
    model = ZImageTurbo(quantize=4, model_path=settings.image_model)
    seed = int.from_bytes(workdir.name.encode()[:4], "big")
    for index, scene in enumerate(story.scenes):
        path = image_dir / f"{index:03}.png"
        if valid_image(path):
            continue
        image = model.generate_image(
            seed=seed + index, prompt=scene.image_prompt,
            num_inference_steps=9, width=576, height=1024,
        )
        temp = path.with_name(f"{index:03}.partial.png")
        image.save(str(temp))
        os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["plan", "speech", "images", "edit-speech"])
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.workdir / "settings.json")
    {"plan": generate_plan, "speech": generate_speech, "images": generate_images,
     "edit-speech": generate_edit_speech}[
        args.stage](settings, args.workdir)


if __name__ == "__main__":
    main()
