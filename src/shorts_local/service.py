import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .collector import run_collection
from .config import initialize
from .news_collector import run_news_collection
from .pipeline import run_next
from .process import BusyError, exclusive_lock
from .store import Store
from .telegram import Telegram, credentials, handle_update

logger = logging.getLogger(__name__)


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Transport URLs contain Telegram credentials and resumable upload capabilities.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def schedule_due(store: Store, channel_id: str = "default", now: datetime | None = None) -> str | None:
    with store.transaction():
        profile = store.profile(channel_id)
        settings = profile.settings
        if not settings.schedule_enabled or profile.paused or store.value("paused", False):
            return None
        zone = ZoneInfo(settings.timezone)
        now = datetime.now(zone) if now is None else now.astimezone(zone)
        if now.strftime("%H:%M") < settings.schedule_time:
            return None
        if settings.production_type == "instagram":
            from .instagram_store import InstagramStore

            return InstagramStore(store).enqueue_next(
                channel_id=channel_id, key=f"schedule:{now.date().isoformat()}")
        if settings.production_type == "news":
            from .news_store import NewsStore

            return NewsStore(store).enqueue_next(
                channel_id=channel_id, key=f"schedule:{now.date().isoformat()}")
        return store.enqueue(channel_id=channel_id, key=f"schedule:{now.date().isoformat()}")


def process_telegram(store: Store, bot: Telegram):
    for item in bot.poll(store.value("telegram_offset", 0)):
        handle_update(store, item, bot.user_id)
        # Also acknowledge ignored/unauthorized messages; accepted replies are already durable.
        store.set_value("telegram_offset", max(store.value("telegram_offset", 0), item["update_id"] + 1))
    for note in store.pending_notifications():
        reply = {"text": note["text"]}
        if note["video"]:
            reply["video"] = note["video"]
        try:
            bot.send(reply)
            store.notification_result(note["id"], True)
        except (RuntimeError, OSError):
            store.notification_result(note["id"], False)


def serve(root: Path, stop_event=None):
    initialize(root)
    store = Store(root)
    auth = credentials(root)
    bot = Telegram(*auth) if auth else None
    with (exclusive_lock(root / "service.lock"), ThreadPoolExecutor(max_workers=1) as pool,
          ThreadPoolExecutor(max_workers=1) as collector_pool,
          ThreadPoolExecutor(max_workers=1) as news_pool):
        future, collection, news_collection = None, None, None
        try:
            while stop_event is None or not stop_event.is_set():
                for profile in store.profiles():
                    schedule_due(store, profile.id)
                if future is None or future.done():
                    if future:
                        try:
                            future.result()
                        except BusyError:
                            pass
                        except Exception as exc:
                            logger.error("작업 실행 오류: %s", type(exc).__name__)
                    future = pool.submit(run_next, root)
                if collection is None or collection.done():
                    if collection:
                        try:
                            collection.result()
                        except Exception as exc:
                            logger.error("수집 실행 오류: %s", type(exc).__name__)
                    collection = collector_pool.submit(run_collection, root)
                if news_collection is None or news_collection.done():
                    if news_collection:
                        try:
                            news_collection.result()
                        except Exception as exc:
                            logger.error("뉴스 수집 실행 오류: %s", type(exc).__name__)
                    news_collection = news_pool.submit(run_news_collection, root)
                if bot:
                    try:
                        process_telegram(store, bot)
                    except RuntimeError:
                        logger.warning("텔레그램 연결 재시도 대기")
                        time.sleep(5)
                else:
                    time.sleep(2)
        except KeyboardInterrupt:
            logger.info("진행 중인 작업이 끝나면 종료합니다.")
        finally:
            if bot:
                bot.close()
