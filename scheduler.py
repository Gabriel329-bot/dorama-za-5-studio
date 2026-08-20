"""Разовая проверка расписания. Вызывается по расписанию из Windows Task Scheduler
(например, раз в 15 минут) — не является постоянно висящим процессом."""
from datetime import datetime, timedelta, timezone

from settings import CONFIG
from storage import db

WINDOW_MINUTES = 20  # насколько "опаздание" от точного времени слота ещё считается допустимым


def check_and_publish() -> None:
    db.init_db()
    if CONFIG.get("dorama", {}).get("require_review", True):
        print("Ручная проверка включена: автоматическая публикация из очереди пропущена")
        return
    now = datetime.now(timezone.utc).astimezone()
    today = now.strftime("%Y-%m-%d")

    for platform in ("telegram", "youtube"):
        platform_cfg = CONFIG["publishing"][platform]
        if not platform_cfg["enabled"]:
            continue
        for slot in platform_cfg["post_times"]:
            hh, mm = map(int, slot.split(":"))
            slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not (slot_dt <= now <= slot_dt + timedelta(minutes=WINDOW_MINUTES)):
                continue
            if not db.try_claim_platform_slot(today, slot, platform):
                continue
            try:
                if platform == "telegram":
                    from publisher import telegram
                    publish_result: object = telegram.publish_next()
                else:
                    from publisher import youtube
                    publish_result = youtube.publish_next(privacy_status=platform_cfg["privacy_status"])
            except BaseException:
                db.release_platform_slot(today, slot, platform)
                raise
            message = "Опубликовано" if publish_result else "Очередь пуста"
            print(f"[{now:%Y-%m-%d %H:%M}] {platform} {slot}: {message}")


if __name__ == "__main__":
    check_and_publish()
