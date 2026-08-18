"""Публикация клипов из очереди в Telegram-канал через Bot API."""
import shutil
from pathlib import Path

import requests

from settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, POSTED_DIR
from storage import db

TELEGRAM_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # лимит Bot API для sendVideo


def _send_video(file_path: Path, caption: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID не заданы в .env")

    if file_path.stat().st_size > TELEGRAM_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"{file_path.name} весит больше 50MB — Telegram Bot API не примет файл напрямую."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "caption": caption[:1024],
                "supports_streaming": True,
            },
            files={"video": (file_path.name, f, "video/mp4")},
            timeout=120,
        )

    if not resp.ok or not resp.json().get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {resp.status_code} {resp.text}")


def publish_next() -> bool:
    """Публикует самый старый клип из очереди. Возвращает False, если очередь пуста."""
    db.init_db()
    clip = db.get_oldest_pending()
    if clip is None:
        return False

    file_path = Path(clip["file_path"])
    if not file_path.exists():
        raise FileNotFoundError(f"В очереди есть запись на {file_path}, но файла нет на диске")

    _send_video(file_path, clip["caption"])

    POSTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = POSTED_DIR / file_path.name
    shutil.move(str(file_path), str(dest))

    db.mark_posted(clip["id"], platform="telegram")
    return True
