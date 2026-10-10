import json
import os
import re
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path

from .config import Settings
from .models import Story
from .process import BlockedError, QuotaError, atomic_json, run_process


def parse_json(text: str) -> dict:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    elif "<think>" in text:
        raise ValueError("AI 기획이 끝나기 전에 응답이 중단되었습니다.")
    # raw_decode respects braces inside JSON strings, unlike a greedy regex.
    start = text.find("{")
    if start < 0:
        raise ValueError("AI 응답에 대본 JSON이 없습니다.")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("AI 응답은 JSON 객체여야 합니다.")
    return data


def parse_story(text: str) -> Story:
    return Story.model_validate(parse_json(text))


def same_title(left: str, right: str) -> bool:
    def normalize(value):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())
    return normalize(left) == normalize(right)


def subscription_env(source: dict | None = None) -> dict:
    return {key: value for key, value in (os.environ if source is None else source).items()
            if not key.startswith("OPENAI_") and key != "CODEX_API_KEY"}


def require_subscription(code: int, status: str) -> None:
    if code != 0 or "logged in using chatgpt" not in status.lower():
        raise BlockedError("Codex의 ChatGPT 로그인이 필요합니다. `codex login` 후 재시도하세요.")


def story_prompt(settings: Settings, recent: list[str]) -> str:
    return f"""You are writing one ORIGINAL fictional Korean YouTube Short.
Return ONLY a JSON object. Do not run tools, read files, or execute commands.
Topic: {settings.topic}
Creative seed (vary the premise): {uuid.uuid4().hex[:8]}
Do not repeat these recent titles: {json.dumps(recent, ensure_ascii=False)}
Write a clear beginning, surprising development, and satisfying ending. No actual news,
medical/financial advice, named real people, or claims of true events. Make all narration
natural spoken Korean. No stage directions, markdown, emojis, or hashtags in narration.
CRITICAL: Tell ONE connected story with ONE protagonist and ONE recurring object.
Every scene must causally follow the previous scene. Stay in one location or explain moves.
The final scene must resolve the mystery raised in the first scene. Avoid unrelated vignettes,
random magical events, and ambiguous sentences. Plan the ending before writing the beginning.
For example, a forgotten umbrella appears each rainy day; a neighbor quietly repairs it;
the owner recognizes the neighbor's stitching; they finally meet and share the umbrella.
Invent your own DIFFERENT premise with the same clear cause-and-effect structure.
Use 5 scenes, each narration 35-65 Korean characters, about 40-60 seconds in total.
The first sentence must create curiosity. Each heading is at most 15 Korean characters.
Image prompts must be detailed English descriptions in a consistent cinematic illustration
style, with no typography or text. Each scene must depict its own narrative moment.
Title must be catchy and under 40 characters. Description states it is a fictional story.
JSON format:
{{"title":"제목", "description":"설명", "scenes":[
{{"heading":"장면 제목", "narration":"한국어 내레이션", "image_prompt":"English prompt"}}
]}}
"""


def generate_draft(settings: Settings, prompt: str, workdir: Path, *, schema: dict,
                   max_tokens: int = 2500) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    settings.save(workdir / "settings.json")
    atomic_json(workdir / "request.json", {"prompt": prompt, "max_tokens": max_tokens})
    output = workdir / "draft.txt"
    if settings.planner == "mlx":
        run_process([sys.executable, "-m", "shorts_local.engines", "plan", str(workdir)],
                    workdir / "plan.log", settings.stage_timeout)
    else:
        env = subscription_env()
        try:
            status = subprocess.run(["codex", "login", "status"], capture_output=True,
                                    text=True, env=env, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise BlockedError("Codex CLI 설치 및 ChatGPT 로그인이 필요합니다.") from exc
        require_subscription(status.returncode, status.stdout + status.stderr)
        atomic_json(workdir / "schema.json", schema)
        # Force subscription auth even if the user's default provider is an API-key provider.
        args = ["codex", "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--output-schema", str(workdir / "schema.json"),
                "--output-last-message", str(output), "-"]
        try:
            run_process(args, workdir / "plan.log", settings.stage_timeout,
                        stdin=prompt, env=env, cwd=workdir)
        except RuntimeError:
            log = (workdir / "plan.log").read_text(errors="replace").lower()
            if any(word in log for word in ["usage limit", "rate limit", "quota exceeded"]):
                raise QuotaError("ChatGPT 구독 한도에 도달했습니다. 무료 할당량 회복을 기다립니다.") from None
            raise
    return output.read_text()


def plan_story(settings: Settings, recent: list[str], workdir: Path) -> Story:
    story = parse_story(generate_draft(settings, story_prompt(settings, recent), workdir,
                                       schema=Story.model_json_schema()))
    if any(same_title(story.title, title) for title in recent):
        raise ValueError("최근 영상과 동일한 제목입니다. 새 대본이 필요합니다.")
    atomic_json(workdir / "story.json", story.model_dump())
    return story
