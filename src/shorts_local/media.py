import json
import subprocess
import sys
import wave
from array import array
from pathlib import Path


def audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path)) as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            channels, width = audio.getnchannels(), audio.getsampwidth()
            if channels not in (1, 2) or width != 2 or not 8000 <= rate <= 96000:
                raise ValueError("음성은 8~96kHz 16-bit PCM, 모노/스테레오 형식이어야 합니다.")
            duration = frames / rate
            if not 0.3 <= duration <= 40:
                raise ValueError("음성 길이가 0.3~40초 범위를 벗어났습니다.")
            payload = audio.readframes(frames)
        if len(payload) != frames * channels * width:
            raise ValueError("음성 샘플이 잘렸거나 헤더와 일치하지 않습니다.")
        samples = array("h", payload)
        if sys.byteorder != "little":
            samples.byteswap()
        if max(abs(sample) for sample in samples) < 4:
            raise ValueError("음성이 무음입니다.")
        return duration
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError(f"유효한 음성 파일이 아닙니다: {path.name}") from exc


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ValueError(f"미디어 파일 검증 실패: {path.name}")
    return json.loads(result.stdout)
