import json
import os
from pathlib import Path

import httpx

from .config import Settings
from .health import heartbeat_age, heartbeat_label
from .instagram_models import parse_instagram_url
from .instagram_store import InstagramStore
from .news_store import NewsStore
from .process import BlockedError, atomic_json
from .store import Store

HELP = """Shorts Local · 로컬 쇼츠 제작기
/channels — 채널 목록·연결·예약·정지 상태
/channel [채널ID] — 채널 선택/현재 채널
/channel_add 채널ID 표시이름 — 채널 추가 (매일 09:00 예약 활성)
/status [all] — 선택 채널/전체 상태와 최근 작업
/create [주제] — 한 편 생성
/create_story [주제] — 이야기 한 편 생성
/source_add 계정명 · /sources — 수집 계정 등록/조회
/source_pause ID · /source_resume ID — 수집원 정지/재개
/source_scan · /materials — 수집 예약/소재 조회
/import URL [narrated|original] — 링크 제작 (URL만 보내도 가능)
/article URL — 기사 링크로 뉴스 제작 (HTTPS URL만 보내도 가능)
/news_add FEED_URL · /news_sources — RSS 등록/조회
/news_pause ID · /news_resume ID — RSS 정지/재개
/news_scan — RSS 확인 예약
/queue [all] — 선택 채널/전체 작업 목록
/preview 작업ID — 완성 영상 받기
/pause · /resume — 선택 채널 정지/재개
/pause all · /resume all — 전체 정지/재개
/retry 작업ID — 실패 작업 재시도
/upload 작업ID — 완성 영상 업로드
/update — 새 릴리즈 확인. 파일은 바꾸지 않음
/settings — 현재 설정
/set topic 새 주제
/set schedule 09:00
/set planner mlx 또는 codex
/set visuals cards 또는 mflux
/set privacy public, unlisted, private
/set autopublish on 또는 off
/set name 채널이름 · /set voice Sohee
/set timezone Asia/Seoul
/set schedule_enabled on 또는 off
/set type story, instagram 또는 news
/set news_style 뉴스 말투 · /set news_poll_minutes 60
/set edit_mode narrated 또는 original
/set edit_style 편집 지침 · /set accent #E8B257
진행 중인 작업은 설정 변경의 영향을 받지 않습니다."""


def handle_update(store: Store, update: dict, owner_id: int) -> dict | None:
    message = update.get("message", {})
    chat = message.get("chat", {})
    if (message.get("from", {}).get("id") != owner_id or chat.get("type") != "private"
            or chat.get("id") != owner_id):
        return None
    text = message.get("text", "").strip()
    if not text:
        return None
    command = text.partition(" ")[0].split("@")[0].lower()
    prepared = _release_notice() if command == "/update" else None
    service = heartbeat_label(heartbeat_age(store.root)) if command in {"/status", "/queue"} else None
    with store.transaction():
        reply = store.telegram_reply(update["update_id"])
        if reply is None:
            reply = {"text": f"[전체]\n{prepared}"} if prepared is not None else _handle_command(
                store, update, text, service=service)
            store.accept_telegram(update["update_id"], reply)
        return reply


def _release_notice() -> str:
    from .updates import UpdateBlocked, installed_version, latest_tag, release_status, version_tuple

    try:
        tag = latest_tag()
        current = installed_version()
        notice = release_status(current, tag)
    except UpdateBlocked as exc:
        return str(exc)
    if version_tuple(tag) > version_tuple(current):
        return f"{notice}\nTelegram에서는 파일을 바꾸지 않습니다."
    return notice


def _handle_command(store: Store, update: dict, text: str, *, service: str | None = None) -> dict:
    try:
        _, url = parse_instagram_url(text)
    except ValueError:
        if text.lower().startswith("https://") and not any(c.isspace() for c in text):
            text = f"/article {text}"
    else:
        text = f"/import {url}"
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()
    arg = arg.strip()
    label = "[전체]"

    def reply(text, video=None):
        result = {"text": f"{label}\n{text}"}
        if video is not None:
            result["video"] = str(video)
        return result

    try:
        if command == "/channels":
            lines = [f"{p.label} · 연결: {p.youtube_channel_id or '미연결'} · 정지: {p.paused}\n"
                     f"예약: {p.settings.schedule_enabled} {p.settings.schedule_time} ({p.settings.timezone})"
                     for p in store.profiles()]
            return reply("\n".join(lines))
        if command == "/channel_add":
            channel_id, _, name = arg.partition(" ")
            p = store.add_profile(channel_id, name)
            label = p.label
            return reply(f"채널 추가 · 예약 활성: {p.settings.schedule_enabled}\n"
                         f"{p.settings.schedule_time} ({p.settings.timezone})")
        if command == "/channel" and arg:
            p = store.profile(arg)
            store.set_value("telegram_channel", p.id)
        p = store.profile(store.value("telegram_channel", "default"))
        label, settings = p.label, p.settings
        instagram = InstagramStore(store)
        news = NewsStore(store)
        if command == "/channel":
            return reply("현재 선택한 채널입니다.")
        if command in {"/start", "/help"}:
            return reply(HELP)
        if command in {"/create", "/create_story", "/import", "/article"}:
            key = f"telegram:{update['update_id']}"
            if command == "/article":
                job = news.import_article(arg, channel_id=p.id, key=key)
            elif command == "/import":
                parts = arg.split()
                if len(parts) not in (1, 2) or len(parts) == 2 and parts[1] not in {"original", "narrated"}:
                    raise ValueError
                job = instagram.import_video(parts[0], channel_id=p.id, mode=parts[1] if len(parts) == 2 else None, key=key)
            elif command == "/create" and settings.production_type == "instagram":
                if arg:
                    return reply("인스타그램 제작에는 이야기 주제를 넣을 수 없습니다. /create_story를 사용하세요.")
                job = instagram.enqueue_next(channel_id=p.id, key=key)
            elif command == "/create" and settings.production_type == "news":
                if arg:
                    return reply("뉴스 제작에는 이야기 주제를 넣을 수 없습니다. /article 또는 /create_story를 사용하세요.")
                job = news.enqueue_next(channel_id=p.id, key=key)
            else:
                options = Settings.model_validate(settings.model_dump() | {"production_type": "story"})
                job = store.enqueue(options, channel_id=p.id, topic=arg or None, key=key)
            if job is None:
                return reply("소재 대기 · 사용 가능한 새 소재가 없습니다.")
            kind = Settings.model_validate(store.get(job)["settings"]).production_label
            return reply(f"{kind} 작업 등록: {job}")
        if command == "/news_add":
            source = news.add_source(arg, channel_id=p.id)
            return reply(f"뉴스 수집원: {source['id']} · {source['status']}")
        if command == "/news_sources":
            return reply("\n".join(f"{s['id']} {s['feed_url']} · {s['status']} · 활성: {bool(s['active'])}"
                                    for s in news.sources(p.id)) or "등록된 피드 없음")
        if command in {"/news_pause", "/news_resume"}:
            source = news.source(arg)
            label = store.profile(source["channel_id"]).label
            news.set_source_active(arg, command == "/news_resume")
            return reply(f"뉴스 수집원 {arg} · {'재개' if command == '/news_resume' else '정지'}")
        if command == "/news_scan":
            news.request_scan(p.id)
            return reply("뉴스 수집 확인을 예약했습니다. 정지/재시도 대기 시각을 따릅니다.")
        if command == "/source_add":
            source = instagram.add_source(arg, channel_id=p.id)
            return reply(f"수집원: {source['id']} @{source['username']} · {source['status']} · 연결 대기")
        if command == "/sources":
            return reply("\n".join(f"{s['id']} @{s['resolved_username'] or s['username']} · {s['status']} · "
                                    f"활성: {bool(s['active'])}" for s in instagram.sources(p.id)) or "등록된 계정 없음")
        if command in {"/source_pause", "/source_resume"}:
            source = instagram.source(arg)
            label = store.profile(source["channel_id"]).label
            instagram.set_source_active(arg, command == "/source_resume")
            return reply(f"수집원 {arg} · {'재개' if command == '/source_resume' else '정지'}")
        if command == "/source_scan":
            instagram.request_scan(p.id)
            return reply("수집 확인을 예약했습니다. 정지/서버 대기 시각을 따릅니다.")
        if command == "/materials":
            return reply("\n".join(f"{i['shortcode']} · {i['status']} · 작업: {i['job_id'] or '-'} "
                                    f"{i['job_status'] or ''} · {i['reason'] or ''}"
                                    for i in instagram.materials(p.id)) or "소재 대기")
        if command in {"/status", "/queue"}:
            if arg not in {"", "all"}:
                raise ValueError
            selected = None if arg == "all" else p.id
            if selected is None:
                label = "[전체]"
            paused = store.value("paused", False) or (selected is not None and p.paused)
            from .updates import installed_version

            lines = [f"버전: {installed_version()}", "일시정지" if paused else "실행 가능",
                     f"기획: {settings.planner} · 화면: {settings.visuals} · 추가 서비스 요금 0원",
                     f"서비스: {service or heartbeat_label(heartbeat_age(store.root))} · "
                     f"미전송 알림: {store.notification_count()}건"]
            for channel in store.profiles() if selected is None else [p]:
                materials = news if channel.settings.production_type == "news" else instagram
                state = materials.channel_state(channel.id)
                lines.append(f"{channel.label} {channel.settings.production_label} · 소재: {state['available']} · "
                             f"{'소재 대기' if not state['available'] else '소재 준비'} · auth_wait: {state['auth_wait']}")
            lines += [f"[{x['channel_id']}] {x['id']} · {Settings.model_validate(x['settings']).production_label} · "
                       f"{x['status']}/{x['stage']} · {x['title'] or '기획 전'}"
                       for x in store.list(10, channel_id=selected)]
            return reply("\n".join(lines))
        if command in {"/pause", "/resume"}:
            if arg not in {"", "all"}:
                raise ValueError
            if arg == "all":
                label = "[전체]"
            store.set_paused(command == "/pause", channel_id=None if arg == "all" else p.id)
            return reply("다음 작업부터 정지합니다." if command == "/pause" else "자동 실행을 재개합니다.")
        if command in {"/preview", "/retry", "/upload"}:
            job = store.get(arg)
            label = store.profile(job["channel_id"]).label + " · " + Settings.model_validate(job["settings"]).production_label
            if command == "/preview":
                video = store.root / "jobs" / job["id"] / "video.mp4"
                if not video.exists():
                    return reply("아직 완성된 영상이 없습니다.")
                return reply(job["title"] or job["id"], video)
            if command == "/upload":
                if job["status"] not in {"ready", "blocked", "failed"}:
                    return reply("완성되었거나 중단된 작업만 업로드할 수 있습니다.")
                store.update(arg, publish=True)
            store.retry(arg)
            return reply(f"작업을 다시 등록했습니다: {arg}")
        if command == "/settings":
            return reply(f"제작: {settings.production_label}\n편집: {settings.instagram_style}\n"
                         f"뉴스: {settings.news_style} · RSS {settings.news_poll_minutes}분\n"
                         f"주제: {settings.topic}\n예약: {settings.schedule_enabled} "
                         f"{settings.schedule_time} ({settings.timezone})\n"
                         f"기획: {settings.planner}\n화면: {settings.visuals}\n목소리: {settings.speech_voice}\n"
                         f"공개: {settings.privacy}\n자동 업로드: {settings.youtube_enabled}")
        if command == "/set":
            key, _, value = arg.partition(" ")
            fields = {"topic": "topic", "schedule": "schedule_time", "planner": "planner",
                       "visuals": "visuals", "privacy": "privacy", "autopublish": "youtube_enabled",
                       "name": "channel_name", "voice": "speech_voice", "timezone": "timezone",
                       "schedule_enabled": "schedule_enabled", "type": "production_type",
                       "edit_mode": "instagram_edit_mode", "edit_style": "instagram_style",
                        "accent": "instagram_accent_color", "news_style": "news_style",
                        "news_poll_minutes": "news_poll_minutes"}
            if key not in fields or not value.strip():
                return reply("설정 형식을 확인하세요. /help")
            if key in {"autopublish", "schedule_enabled"}:
                if value not in {"on", "off"}:
                    return reply(f"{key}는 on 또는 off입니다.")
                value = value == "on"
            updated = store.update_profile_settings(p.id, {fields[key]: value})
            label = updated.label
            return reply("설정을 저장했습니다. 다음 작업부터 적용됩니다.")
    except ValueError:
        return reply("채널·작업 ID 또는 설정값이 올바르지 않습니다. /help")
    return reply("지원하는 명령을 확인하세요. /help")


def credentials(root: Path) -> tuple[str, int] | None:
    data = {}
    path = root / "auth/telegram.json"
    if path.exists():
        data = json.loads(path.read_text())
    token = os.environ.get("SHORTS_TELEGRAM_TOKEN", data.get("token"))
    owner = os.environ.get("SHORTS_TELEGRAM_USER_ID", data.get("user_id"))
    return (token, int(owner)) if token and owner else None


def save_credentials(root: Path, token: str, owner: int):
    path = root / "auth"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path / "telegram.json", {"token": token, "user_id": owner})


class Telegram:
    def __init__(self, token: str, user_id: int, client=None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.user_id = user_id
        self.client = client or httpx.Client(timeout=30)

    def call(self, method: str, **kwargs):
        try:
            response = self.client.post(f"{self.base}/{method}", **kwargs)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("텔레그램 통신 실패. 자동 재연결합니다.") from None
        if not data.get("ok"):
            # Never stringify a Request or HTTPStatusError: its URL contains the bot token.
            raise RuntimeError(f"텔레그램 요청 실패 (HTTP {response.status_code}).")
        return data.get("result")

    def poll(self, offset: int) -> list[dict]:
        return self.call("getUpdates", json={"offset": offset, "timeout": 2,
                                            "allowed_updates": ["message"]})

    def send(self, reply: dict):
        if "video" in reply:
            path = Path(reply["video"])
            if path.stat().st_size > 49 * 1024**2:
                self.send({"text": "영상이 텔레그램 전송 크기를 초과합니다. 로컬 작업 폴더에서 확인하세요."})
                return
            with path.open("rb") as video:
                self.call("sendVideo", data={"chat_id": self.user_id, "caption": reply["text"][:900],
                                             "supports_streaming": "true"},
                          files={"video": ("short.mp4", video, "video/mp4")})
        else:
            self.call("sendMessage", json={"chat_id": self.user_id, "text": reply["text"][:4000]})

    def verify(self):
        result = self.call("getMe", json={})
        if not result.get("is_bot"):
            raise BlockedError("올바른 봇 토큰이 아닙니다.")
        return result["username"]

    def close(self):
        self.client.close()
