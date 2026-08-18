"""Официальная загрузка на YouTube Data API v3 через OAuth 2.0."""
from pathlib import Path
import re
import shutil

from settings import (
    CONFIG,
    POSTED_DIR,
    YOUTUBE_CLIENT_SECRETS_PATH,
    YOUTUBE_TOKEN_PATH,
)
from storage import db

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
VALID_PRIVACY = {"private", "unlisted", "public"}


def _credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = None
    if YOUTUBE_TOKEN_PATH.is_file():
        credentials = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_PATH), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if credentials and credentials.valid:
        return credentials
    if not interactive:
        raise RuntimeError("YouTube OAuth ещё не настроен. Запусти: python cli.py youtube-auth")
    if not YOUTUBE_CLIENT_SECRETS_PATH.is_file():
        raise FileNotFoundError(
            f"Не найден {YOUTUBE_CLIENT_SECRETS_PATH}. Скачай OAuth Client ID типа Desktop app "
            "из Google Cloud и сохрани файл по этому пути."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(YOUTUBE_CLIENT_SECRETS_PATH), SCOPES)
    credentials = flow.run_local_server(port=0, open_browser=True)
    YOUTUBE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    YOUTUBE_TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def _service(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credentials(interactive), cache_discovery=False)


def authorize() -> Path:
    # OAuth-поток уже проверяет полученный токен. Название канала здесь намеренно
    # не читаем: для этого потребовалось бы отдельное разрешение youtube.readonly.
    _credentials(interactive=True)
    return YOUTUBE_TOKEN_PATH


def _hashtags(caption: str) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for value in re.findall(r"#([\wа-яА-ЯёЁ]+)", caption):
        normalized = value.lower()
        if normalized not in seen:
            seen.add(normalized)
            tags.append(value[:30])
    return tags[:15]


def build_metadata(clip, privacy_status: str | None = None) -> dict:
    cfg = CONFIG["publishing"]["youtube"]
    privacy = privacy_status or cfg["privacy_status"]
    if privacy not in VALID_PRIVACY:
        raise ValueError(f"Некорректный privacy_status: {privacy}")
    caption = str(clip["caption"]).strip()
    plain_lines = [line.strip() for line in caption.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    title = (plain_lines[0] if plain_lines else Path(clip["file_path"]).stem).strip()[:100]
    description = caption
    source = str(clip["source_video"] or "")
    if source.startswith("dorama-trends:"):
        urls = [url for url in source.removeprefix("dorama-trends:").split("|") if url]
        if urls:
            description += "\n\nИсточники тренд-сигналов:\n" + "\n".join(urls[:5])
    return {
        "snippet": {
            "title": title,
            "description": description[:5000],
            "tags": _hashtags(caption),
            "categoryId": str(cfg["category_id"]),
            "defaultLanguage": "ru",
            "defaultAudioLanguage": "ru",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }


def preview(clip_id: int) -> dict:
    db.init_db()
    clip = db.get_clip(clip_id)
    if clip is None:
        raise RuntimeError(f"Ролик #{clip_id} не найден")
    if clip["status"] != "pending":
        raise RuntimeError(f"Ролик #{clip_id} имеет статус {clip['status']}, а не pending")
    return build_metadata(clip)


def publish_next(clip_id: int | None = None, privacy_status: str | None = None) -> str | None:
    from googleapiclient.http import MediaFileUpload

    db.init_db()
    clip = db.get_clip(clip_id) if clip_id is not None else db.get_oldest_pending()
    if clip is None:
        return None
    if clip["status"] != "pending":
        raise RuntimeError(f"Ролик #{clip['id']} уже имеет статус {clip['status']}")
    file_path = Path(clip["file_path"])
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл ролика не найден: {file_path}")

    cfg = CONFIG["publishing"]["youtube"]
    media_upload = MediaFileUpload(
        str(file_path),
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = _service().videos().insert(
        part="snippet,status",
        body=build_metadata(clip, privacy_status),
        notifySubscribers=bool(cfg.get("notify_subscribers", False)),
        media_body=media_upload,
    )
    response = None
    try:
        while response is None:
            progress, response = request.next_chunk()
            if progress:
                print(f"  YouTube upload: {progress.progress() * 100:.0f}%")
    finally:
        # MediaFileUpload держит дескриптор до уничтожения объекта. На Windows
        # его нужно закрыть явно до перемещения уже загруженного MP4.
        stream = media_upload.stream()
        if not stream.closed:
            stream.close()
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube не вернул ID загруженного видео: {response}")

    destination_dir = POSTED_DIR / "youtube"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / file_path.name
    # Сначала фиксируем внешний успех, чтобы локальная ошибка не привела к
    # повторной загрузке того же ролика при следующем запуске.
    db.mark_posted(clip["id"], platform=f"youtube:{video_id}")
    try:
        shutil.move(str(file_path), str(destination))
    except OSError as exc:
        raise RuntimeError(
            f"Видео загружено как {video_id}, но локальный файл не перемещён: {exc}"
        ) from exc
    db.update_file_path(clip["id"], str(destination))
    return video_id
