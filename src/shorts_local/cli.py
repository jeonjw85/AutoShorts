import importlib.util
import json
import os
import platform
import plistlib
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .config import Settings, initialize
from .instagram_store import InstagramStore
from .models import Story
from .news_store import NewsStore
from .pipeline import run_next
from .process import BlockedError, BusyError, atomic_json, exclusive_lock
from .profiles import UploadTarget
from .service import configure_logging, serve
from .store import Store

app = typer.Typer(help="MLX 우선 · 추가 서비스 요금 0원 · 로컬 쇼츠 자동화",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
channel_app = typer.Typer(help="채널별 프로필 관리", no_args_is_help=True)
app.add_typer(channel_app, name="channel")
source_app = typer.Typer(help="인스타그램 수집 계정 관리", no_args_is_help=True)
app.add_typer(source_app, name="source")
news_source_app = typer.Typer(help="뉴스 RSS 수집원 관리", no_args_is_help=True)
app.add_typer(news_source_app, name="news-source")


def profile_for(store: Store, channel: str):
    try:
        return store.profile(channel)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def selection(channel: str | None, all_channels: bool) -> str | None:
    if all_channels and channel is not None:
        raise typer.BadParameter("--all과 --channel은 함께 사용할 수 없습니다.")
    return None if all_channels else channel or "default"


@channel_app.command("list")
def channel_list(ctx: typer.Context):
    """채널별 연결·예약·정지 상태를 표시합니다."""
    for p in Store(ctx.obj).profiles():
        typer.echo(f"{p.label} · YouTube: {p.youtube_channel_id or '미연결'} · "
                   f"예약: {p.settings.schedule_enabled} {p.settings.schedule_time} "
                   f"({p.settings.timezone}) · 정지: {p.paused} · {p.settings.production_label}")


@channel_app.command("add")
def channel_add(ctx: typer.Context, channel_id: str, name: Annotated[str, typer.Option()]):
    """독립적인 기본 설정으로 새 채널을 추가합니다."""
    try:
        p = Store(ctx.obj).add_profile(channel_id, name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} 추가됨 · 예약: {p.settings.schedule_enabled} "
               f"{p.settings.schedule_time} ({p.settings.timezone})")


def _show_version(value: bool):
    if value:
        from .updates import installed_version

        typer.echo(installed_version())
        raise typer.Exit()


@app.callback()
def main(ctx: typer.Context, home: Annotated[Path, typer.Option(envvar="SHORTS_HOME")] = Path("data"),
         version: Annotated[bool, typer.Option("--version", callback=_show_version,
                                                is_eager=True)] = False):
    ctx.obj = home.expanduser().resolve()


@app.command()
def init(ctx: typer.Context):
    """기본 설정과 작업 DB를 생성합니다. 기존 설정은 보존합니다."""
    root = ctx.obj
    initialize(root)
    Store(root)
    typer.echo(f"준비됨: {root}\n다음: shorts-local doctor")


@app.command()
def doctor(ctx: typer.Context):
    """설치 상태를 확인합니다. 네트워크 호출이나 모델 로딩을 하지 않습니다."""
    root = ctx.obj
    settings = initialize(root)
    store = Store(root)
    from .youtube import auth_path, read_auth

    channels = []
    for p in store.profiles():
        connected = False
        if p.youtube_channel_id:
            try:
                connected = read_auth(auth_path(root, p.id)).get("youtube_channel_id") in (
                    None, p.youtube_channel_id)
            except BlockedError:
                pass
        channels.append({"id": p.id, "name": p.settings.channel_name, "paused": p.paused,
                         "youtube_connected": connected, "youtube_channel_id": p.youtube_channel_id})
    from .updates import installed_version

    report = {
        "version": installed_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "free_disk_gb": round(shutil.disk_usage(root).free / 1024**3, 1),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "mlx_lm": importlib.util.find_spec("mlx_lm") is not None,
        "mlx_audio": importlib.util.find_spec("mlx_audio") is not None,
        "mflux_optional": importlib.util.find_spec("mflux") is not None,
        "youtube_connected": next(p["youtube_connected"] for p in channels if p["id"] == "default"),
        "channels": channels,
        "telegram_configured": (root / "auth/telegram.json").exists()
        or bool(os.environ.get("SHORTS_TELEGRAM_TOKEN")),
        "planner": settings.planner, "visuals": settings.visuals,
        "instagram": {
            "instaloader_installed": importlib.util.find_spec("instaloader") is not None,
            "whisper_installed": importlib.util.find_spec("mlx_whisper") is not None,
            "vlm_installed": importlib.util.find_spec("mlx_vlm") is not None,
            "session_present": (root / "auth/instagram.json").is_file(),
            "auth_wait": bool(store.value("instagram_auth_wait", False)),
            "next_run": store.value("instagram_next_run", 0),
        },
        "additional_service_budget": 0,
    }
    if importlib.util.find_spec("huggingface_hub"):
        from huggingface_hub import try_to_load_from_cache
        for name, model in (("text_model", settings.text_model), ("speech_model", settings.speech_model),
                            ("transcription_model", settings.transcription_model), ("vision_model", settings.vision_model)):
            report[f"{name}_cached"] = isinstance(try_to_load_from_cache(model, "config.json"), str)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not (report["ffmpeg"] and report["ffprobe"]):
        raise typer.Exit(1)


@app.command()
def models(ctx: typer.Context, images: bool = False, instagram: bool = False, channel: str = "default"):
    """무료 공개 모델 준비. --images는 이미지, --instagram은 전사·장면 분석도 받습니다."""
    if importlib.util.find_spec("huggingface_hub") is None:
        raise typer.BadParameter("먼저 uv sync --extra mlx를 실행하세요.")
    from huggingface_hub import snapshot_download

    settings = profile_for(Store(ctx.obj), channel).settings
    minimum = (20 if images else 10) + (10 if instagram else 0)
    if shutil.disk_usage(ctx.obj).free < minimum * 1024**3:
        raise typer.BadParameter(f"다운로드를 위해 최소 {minimum}GB 여유 공간을 확보하세요.")
    repos = [settings.text_model, settings.speech_model]
    if images:
        repos.append(settings.image_model)
    if instagram:
        repos += [settings.transcription_model, settings.vision_model]
    for repo in dict.fromkeys(repos):
        typer.echo(f"다운로드/캐시 확인: {repo}")
        snapshot_download(repo, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model",
                                                "*.jinja", "*.tiktoken", "*.npy", "*.npz"])
    typer.echo("모델 준비 완료")


@app.command()
def config(ctx: typer.Context, key: Annotated[str | None, typer.Argument()] = None,
           value: Annotated[str | None, typer.Argument()] = None,
           channel: str = "default",
           values: Annotated[str | None, typer.Option(help="여러 설정을 JSON 객체로 한 번에 변경")] = None):
    """설정 조회/변경. 예: config topic '우주 우체부의 창작 이야기'"""
    store = Store(ctx.obj)
    profile = profile_for(store, channel)
    settings = profile.settings
    if values is not None:
        if key is not None or value is not None:
            raise typer.BadParameter("--values는 개별 key/value와 함께 사용할 수 없습니다.")
        try:
            changes = json.loads(values)
        except ValueError:
            raise typer.BadParameter("--values에는 JSON 객체가 필요합니다.") from None
        if not isinstance(changes, dict):
            raise typer.BadParameter("--values에는 JSON 객체가 필요합니다.")
    elif key is None:
        typer.echo(json.dumps({"channel_id": channel, **settings.model_dump()}, ensure_ascii=False, indent=2))
        return
    else:
        if key not in Settings.model_fields:
            raise typer.BadParameter("지원하지 않는 설정 이름입니다.")
        if value is None:
            typer.echo(str(getattr(settings, key)))
            return
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        changes = {key: parsed}
    try:
        store.update_profile_settings(channel, changes)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{profile.label} 저장했습니다. 다음 작업부터 적용됩니다.")


@app.command()
def create(ctx: typer.Context, topic: str | None = None,
            story: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
             upload: bool = False, local_only: bool = False, channel: str = "default",
             production_type: Annotated[str | None, typer.Option("--type")] = None,
             mode: str | None = None):
    """작업을 등록합니다. --story는 직접 작성한 대본, --local-only는 로컬 제작만."""
    store = Store(ctx.obj)
    profile = profile_for(store, channel)
    try:
        kind = production_type or ("story" if story else profile.settings.production_type)
        if (upload and local_only or kind not in {"story", "instagram", "news"}
                or kind in {"instagram", "news"} and (topic is not None or story is not None)
                or kind != "instagram" and mode is not None
                or mode is not None and mode not in {"narrated", "original"}):
            raise ValueError("제작 유형·모드·대본·게시 옵션의 조합이 올바르지 않습니다.")
        publish = False if local_only else True if upload else None
        document = Story.model_validate_json(story.read_text()) if story else None
        if kind == "instagram":
            job = InstagramStore(store).enqueue_next(channel_id=channel, mode=mode, publish=publish)
        elif kind == "news":
            job = NewsStore(store).enqueue_next(channel_id=channel, publish=publish)
        else:
            settings = Settings.model_validate(profile.settings.model_dump() | {"production_type": "story"})
            job = store.enqueue(settings, channel_id=channel, topic=topic, publish=publish, story=document)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if job is None:
        typer.echo(f"{profile.label} 소재 대기 · 사용 가능한 새 소재가 없습니다.")
        return
    kind = Settings.model_validate(store.get(job)["settings"]).production_label
    typer.echo(f"{profile.label} {kind} 작업 등록: {job}\n처리: shorts-local work 또는 shorts-local serve")


@app.command("import-video")
def import_video(ctx: typer.Context, url: str, channel: str = "default", mode: str | None = None,
                 upload: bool = False, local_only: bool = False):
    """직접 보낸 인스타그램 링크를 즉시 큐에 등록합니다."""
    store = Store(ctx.obj)
    p = profile_for(store, channel)
    if upload and local_only or mode is not None and mode not in {"narrated", "original"}:
        raise typer.BadParameter("게시 옵션 또는 편집 모드를 확인하세요.")
    try:
        job = InstagramStore(store).import_video(url, channel_id=channel, mode=mode,
                                                 publish=False if local_only else True if upload else None)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} {Settings.model_validate(store.get(job)['settings']).production_label} 작업: {job}")


@app.command("instagram-login")
def instagram_login(ctx: typer.Context, username: str):
    """로컬에서 Instagram에 로그인합니다. 비밀번호와 2FA 입력은 숨겨집니다."""
    from .instagram import InstagramError, login_instagram

    if importlib.util.find_spec("instaloader") is None:
        raise typer.BadParameter("uv sync --extra mlx --extra images --extra instagram으로 설치하세요.")
    try:
        name = login_instagram(Store(ctx.obj), username,
                               prompt_secret=lambda message: typer.prompt(message, hide_input=True))
    except (InstagramError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"[공유 수집 계정] @{name} 연결 완료 · 인증 대기 작업을 복원했습니다.")


@app.command("import-article")
def import_article(ctx: typer.Context, url: str, channel: str = "default",
                   upload: bool = False, local_only: bool = False):
    """기사 HTTPS 링크를 뉴스 작업으로 등록합니다."""
    store = Store(ctx.obj)
    p = profile_for(store, channel)
    if upload and local_only:
        raise typer.BadParameter("--upload와 --local-only는 함께 사용할 수 없습니다.")
    try:
        job = NewsStore(store).import_article(url, channel_id=channel,
                                             publish=False if local_only else True if upload else None)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} news 작업: {job}")


@news_source_app.command("add")
def news_source_add(ctx: typer.Context, url: str, channel: str = "default"):
    store = Store(ctx.obj)
    p = profile_for(store, channel)
    try:
        source = NewsStore(store).add_source(url, channel_id=channel)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} 뉴스 수집원: {source['id']} · {source['status']}")


@news_source_app.command("list")
def news_source_list(ctx: typer.Context, channel: str = "default"):
    store = Store(ctx.obj)
    typer.echo(profile_for(store, channel).label)
    for source in NewsStore(store).sources(channel):
        typer.echo(f"{source['id']} {source['feed_url']} · {source['status']} · 활성: {bool(source['active'])} · "
                   f"다음 확인: {source['next_run']}")


def _news_source_active(root, source_id, active):
    store = Store(root)
    news = NewsStore(store)
    try:
        source = news.source(source_id)
        news.set_source_active(source_id, active)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{store.profile(source['channel_id']).label} {source_id} · {'재개' if active else '정지'}")


@news_source_app.command("pause")
def news_source_pause(ctx: typer.Context, source_id: str):
    _news_source_active(ctx.obj, source_id, False)


@news_source_app.command("resume")
def news_source_resume(ctx: typer.Context, source_id: str):
    _news_source_active(ctx.obj, source_id, True)


@news_source_app.command("scan")
def news_source_scan(ctx: typer.Context, channel: str = "default"):
    """허용된 뉴스 피드 하나를 최대 120초 확인합니다."""
    from .news_collector import run_news_collection

    store = Store(ctx.obj)
    p = profile_for(store, channel)
    NewsStore(store).request_scan(channel)
    result = run_news_collection(ctx.obj, channel_id=channel)
    typer.echo(f"{p.label} 뉴스 수집 확인: {result or '대기/정지 또는 등록된 피드 없음'}")


@source_app.command("add")
def source_add(ctx: typer.Context, username: str, channel: str = "default"):
    store = Store(ctx.obj)
    p = profile_for(store, channel)
    try:
        source = InstagramStore(store).add_source(username, channel_id=channel)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} 수집원: {source['id']} @{source['username']} · {source['status']} · 연결 대기")


@source_app.command("list")
def source_list(ctx: typer.Context, channel: str = "default"):
    store = Store(ctx.obj)
    typer.echo(profile_for(store, channel).label)
    for source in InstagramStore(store).sources(channel):
        typer.echo(f"{source['id']} @{source['resolved_username'] or source['username']} · {source['status']} · "
                   f"활성: {bool(source['active'])} · 다음 확인: {source['next_run']}")


def _source_active(root, source_id, active):
    store = Store(root)
    instagram = InstagramStore(store)
    try:
        source = instagram.source(source_id)
        instagram.set_source_active(source_id, active)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{store.profile(source['channel_id']).label} {source_id} · {'재개' if active else '정지'}")


@source_app.command("pause")
def source_pause(ctx: typer.Context, source_id: str):
    _source_active(ctx.obj, source_id, False)


@source_app.command("resume")
def source_resume(ctx: typer.Context, source_id: str):
    _source_active(ctx.obj, source_id, True)


@source_app.command("scan")
def source_scan(ctx: typer.Context, channel: str = "default"):
    """허용된 수집 계정 하나를 최대 120초 확인합니다."""
    from .collector import run_collection

    store = Store(ctx.obj)
    p = profile_for(store, channel)
    InstagramStore(store).request_scan(channel)
    result = run_collection(ctx.obj, channel_id=channel)
    typer.echo(f"{p.label} 수집 확인: {result or '대기/정지 또는 등록된 계정 없음'}")


@app.command()
def materials(ctx: typer.Context, channel: str = "default"):
    store = Store(ctx.obj)
    typer.echo(profile_for(store, channel).label)
    items = InstagramStore(store).materials(channel)
    if not items:
        typer.echo("소재 대기")
    for item in items:
        typer.echo(f"{item['shortcode']} · {item['status']} · 작업: {item['job_id'] or '-'} "
                   f"{item['job_status'] or ''} · {item['reason'] or ''}")


@app.command()
def work(ctx: typer.Context):
    """큐에서 실행 가능한 작업 한 개를 처리하고 종료합니다."""
    initialize(ctx.obj)
    try:
        job_id = run_next(ctx.obj)
    except BusyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    if job_id is None:
        typer.echo("실행할 작업이 없거나 일시정지 상태입니다.")
        return
    job = Store(ctx.obj).get(job_id)
    typer.echo(f"{Store(ctx.obj).profile(job['channel_id']).label} {job_id}: {job['status']} / {job['stage']}")
    if job["status"] in {"ready", "uploaded"}:
        typer.echo(str(ctx.obj / "jobs" / job_id / "video.mp4"))
    else:
        typer.echo(job["error"] or "다음 실행을 기다립니다.")
        raise typer.Exit(1)


@app.command("serve")
def daemon(ctx: typer.Context):
    """예약·제작·텔레그램을 계속 실행합니다. 현재 작업 완료 후 Ctrl+C로 종료."""
    configure_logging()
    try:
        serve(ctx.obj)
    except BusyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None


@app.command()
def status(ctx: typer.Context, channel: str | None = None,
           all_channels: Annotated[bool, typer.Option("--all")] = False):
    """최근 작업과 오류를 확인합니다."""
    store = Store(ctx.obj)
    selected = selection(channel, all_channels)
    profile = profile_for(store, selected) if selected is not None else None
    paused = store.value("paused", False) or (profile is not None and profile.paused)
    typer.echo(f"{profile.label if profile else '[전체]'} {'일시정지' if paused else '실행 가능'}")
    for p in [profile] if profile else store.profiles():
        materials = NewsStore(store) if p.settings.production_type == "news" else InstagramStore(store)
        state = materials.channel_state(p.id)
        typer.echo(f"{p.label} {p.settings.production_label} · 소재: {state['available']} · "
                   f"{'소재 대기' if not state['available'] else '소재 준비'} · auth_wait: {state['auth_wait']} · "
                   f"다음 수집: {state['next_run']}")
    for job in store.list(channel_id=selected):
        typer.echo(f"[{job['channel_id']}] {job['id']} {Settings.model_validate(job['settings']).production_label} "
                   f"{job['status']:10} {job['stage']:8} {job['title'] or ''}")
        if job["error"]:
            typer.echo(f"  {job['error']}")


@app.command()
def pause(ctx: typer.Context, channel: str | None = None,
          all_channels: Annotated[bool, typer.Option("--all")] = False):
    """다음 작업부터 정지합니다."""
    selection(channel, all_channels)
    store = Store(ctx.obj)
    label = profile_for(store, channel).label if channel else "[전체]"
    store.set_paused(True, channel_id=channel)
    typer.echo(f"{label} 일시정지")


@app.command()
def resume(ctx: typer.Context, channel: str | None = None,
           all_channels: Annotated[bool, typer.Option("--all")] = False):
    """정지했던 큐와 예약을 재개합니다."""
    selection(channel, all_channels)
    store = Store(ctx.obj)
    label = profile_for(store, channel).label if channel else "[전체]"
    store.set_paused(False, channel_id=channel)
    typer.echo(f"{label} 재개")


@app.command()
def retry(ctx: typer.Context, job_id: str):
    """완성된 단계는 재사용하고 실패한 작업을 다시 큐에 넣습니다."""
    try:
        store = Store(ctx.obj)
        store.retry(job_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{store.profile(store.get(job_id)['channel_id']).label} 재등록: {job_id}")


@app.command()
def publish(ctx: typer.Context, job_id: str):
    """기존 로컬 완성본을 업로드 큐에 넣습니다."""
    store = Store(ctx.obj)
    with store.transaction():
        job = store.get(job_id)
        if job["status"] not in {"ready", "blocked", "failed"}:
            raise typer.BadParameter("완성되었거나 중단된 작업만 업로드할 수 있습니다.")
        store.update(job_id, publish=True)
        store.retry(job_id)
    typer.echo(f"{store.profile(job['channel_id']).label} 업로드 큐에 등록했습니다. shorts-local work")


@app.command()
def youtube_login(ctx: typer.Context, client_file: Annotated[Path, typer.Argument(exists=True)],
                  channel: str = "default"):
    """Google Desktop OAuth JSON으로 연결하고 이후 작업의 자동 업로드를 켭니다."""
    from .youtube import login

    store = Store(ctx.obj)
    profile_for(store, channel)
    try:
        identity = login(store, channel, client_file)
    except (ValueError, BlockedError, BusyError) as exc:
        raise typer.BadParameter(str(exc)) from None
    p = store.profile(channel)
    typer.echo(f"{p.label} YouTube 연결: {identity.title} ({identity.id}). 새 작업 자동 업로드: {p.settings.privacy}")


@app.command()
def telegram_setup(ctx: typer.Context):
    """봇 토큰과 제어할 사용자 ID를 입력합니다. 토큰은 화면에 표시하지 않습니다."""
    from .telegram import Telegram, save_credentials

    initialize(ctx.obj)
    token = typer.prompt("BotFather 봇 토큰", hide_input=True)
    owner = typer.prompt("내 Telegram 숫자 사용자 ID", type=int)
    if owner <= 0:
        raise typer.BadParameter("개인 사용자 ID는 양수입니다.")
    bot = Telegram(token, owner)
    try:
        username = bot.verify()
        save_credentials(ctx.obj, token, owner)
    except (RuntimeError, BlockedError) as exc:
        raise typer.BadParameter(str(exc)) from None
    finally:
        bot.close()
    typer.echo(f"연결 준비: @{username}\n텔레그램에서 /start를 보낸 뒤 serve를 실행하세요.")


@app.command()
def reconcile(ctx: typer.Context, job_id: str, video_id: str):
    """Studio에서 직접 확인한 영상 ID로 불확실한 업로드 결과를 등록합니다."""
    from .youtube import file_hash

    store = Store(ctx.obj)
    try:
        with exclusive_lock(ctx.obj / "worker.lock"):
            with store.transaction():
                job = store.get(job_id)
                folder = ctx.obj / "jobs" / job_id
                if (job["status"] not in {"blocked", "failed"} or job["stage"] != "upload"
                        or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)):
                    raise typer.BadParameter("업로드 중단 작업과 Studio에서 확인한 11자리 영상 ID가 필요합니다.")
                p = store.profile(job["channel_id"])
                if p.youtube_channel_id is None:
                    raise typer.BadParameter("작업의 원래 YouTube 채널을 먼저 연결하세요.")
                digest = file_hash(folder / "video.mp4")
                target = UploadTarget(channel_id=p.id,
                                      youtube_id=store.bind_job_target(job_id, p.youtube_channel_id))
            # Commit the immutable target before the receipt can survive a crash.
            atomic_json(folder / "upload-receipt.json", {
                "video_id": video_id, "sha256": digest, "manually_reconciled": True,
                **target.artifact_fields(),
            })
            store.update(job_id, status="uploaded", stage="complete", video_id=video_id, error=None)
    except (BusyError, BlockedError) as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{p.label} 등록 완료: https://youtu.be/{video_id}")


@app.command()
def update(ctx: typer.Context):
    """최신 GitHub 릴리즈를 확인하고, 깨끗한 checkout이면 적용합니다."""
    from . import updates

    try:
        tag = updates.latest_tag()
        current = updates.installed_version()
        if updates.version_tuple(tag) <= updates.version_tuple(current):
            typer.echo(updates.release_status(current, tag))
            return
        updates.apply_release(updates.project_root(), ctx.obj, tag)
    except updates.UpdateBlocked as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{tag}를 적용했습니다. 실행 중인 명령은 다시 시작하세요.")


@app.command()
def service_file(ctx: typer.Context):
    """로그인 시 실행할 macOS LaunchAgent 파일을 로컬 데이터 폴더에 생성합니다."""
    root = ctx.obj
    initialize(root)
    data = {
        "Label": "dev.shorts-local",
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", sys.executable, "-m", "shorts_local.cli",
                             "--home", str(root), "serve"],
        "WorkingDirectory": str(Path.cwd()),
        "EnvironmentVariables": {"PATH": f"{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "StandardOutPath": str(root / "service.log"), "StandardErrorPath": str(root / "service.log"),
    }
    path = root / "dev.shorts-local.plist"
    path.write_bytes(plistlib.dumps(data))
    path.chmod(0o600)
    typer.echo(f"생성됨: {path}\n설치는 README의 자동 실행 절차를 참고하세요.")


if __name__ == "__main__":
    app()
