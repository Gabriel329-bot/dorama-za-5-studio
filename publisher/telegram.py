"""Публикация клипов из очереди в Telegram-канал через Bot API."""
from pathlib import Path

import requests

from settings import POSTED_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
from storage import db
from storage.files import move_to_unique

TELEGRAM_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # лимит Bot API для sendVideo


def _send_video(file_path: Path, caption: str) -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID не заданы в .env")

    if file_path.stat().st_size > TELEGRAM_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"{file_path.name} весит больше 50MB — Telegram Bot API не примет файл напрямую."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    try:
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
    except requests.RequestException as exc:
        # requests включает полный URL в исключение, а URL Bot API содержит токен.
        raise RuntimeError(
            f"Сетевая ошибка Telegram Bot API ({type(exc).__name__})"
        ) from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram API вернул некорректный ответ HTTP {resp.status_code}") from exc
    if not resp.ok or not payload.get("ok"):
        description = str(payload.get("description") or "неизвестная ошибка")[:500]
        raise RuntimeError(f"Telegram API: HTTP {resp.status_code}: {description}")
    message_id = payload.get("result", {}).get("message_id")
    if not isinstance(message_id, int):
        raise RuntimeError(  # noqa: TRY004 — нарушение внешнего API-контракта
            "Telegram API не вернул message_id опубликованного видео"
        )
    return message_id


def publish_next() -> bool:
    """Публикует самый старый клип из очереди. Возвращает False, если очередь пуста."""
    db.init_db()
    clip = db.claim_pending("telegram")
    if clip is None:
        return False
    claim_token = str(clip["claim_token"])

    file_path = Path(clip["file_path"])
    if not file_path.exists():
        db.release_claim(int(clip["id"]), claim_token, f"Файл не найден: {file_path}")
        raise FileNotFoundError(f"В очереди есть запись на {file_path}, но файла нет на диске")

    try:
        message_id = _send_video(file_path, str(clip["caption"]))
    except BaseException as exc:
        db.release_claim(int(clip["id"]), claim_token, str(exc))
        raise

    # Внешний успех фиксируется первым: локальная ошибка перемещения не должна
    # приводить к повторной публикации сообщения.
    db.mark_posted_claimed(
        int(clip["id"]), claim_token, platform=f"telegram:{message_id}"
    )
    try:
        destination = move_to_unique(file_path, POSTED_DIR / "telegram")
    except OSError as exc:
        raise RuntimeError(
            f"Видео опубликовано в Telegram как сообщение {message_id}, "
            f"но локальный файл не перемещён: {exc}"
        ) from exc
    db.update_file_path(int(clip["id"]), str(destination))
    return True
