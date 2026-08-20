"""Точка входа: python cli.py clip <video> | publish | status"""
import argparse
import sys
from pathlib import Path

from storage import db
from storage.files import move_to_unique


def cmd_clip(args: argparse.Namespace) -> None:
    from clipper.pipeline import process_video

    created = process_video(args.video)
    if created:
        print(f"\nГотово: {len(created)} клип(ов) добавлено в очередь (output/pending/).")
    else:
        print("\nНичего не добавлено.")


def cmd_publish(args: argparse.Namespace) -> None:
    from publisher import telegram

    posted = telegram.publish_next()
    print("Опубликовано" if posted else "Очередь пуста — публиковать нечего")


def cmd_status(args: argparse.Namespace) -> None:
    db.init_db()
    pending = db.count_pending()
    print(f"В очереди на публикацию: {pending}\n")
    print("Последние записи:")
    for row in db.recent(limit=10):
        print(f"  #{row['id']:<3} [{row['status']:8s}] {row['created_at'][:16]}  {row['caption'][:60]}")


def cmd_reject(args: argparse.Namespace) -> None:
    from settings import REJECTED_DIR

    db.init_db()
    clip = db.claim_pending("cli-reject", clip_id=args.id)
    if clip is None:
        raise RuntimeError(f"Запись #{args.id} не найдена или уже обрабатывается")
    claim_token = str(clip["claim_token"])
    source = Path(clip["file_path"])
    destination = source
    moved = False
    try:
        if source.is_file():
            destination = move_to_unique(source, REJECTED_DIR)
            moved = True
        db.mark_rejected_claimed(args.id, claim_token, str(destination))
    except BaseException as exc:
        if moved and destination.is_file() and not source.exists():
            destination.replace(source)
            moved = False
        if not moved:
            db.release_claim(args.id, claim_token, str(exc))
        raise
    print(f"Ролик #{args.id} отклонён и перемещён в {REJECTED_DIR}")


def cmd_recover(args: argparse.Namespace) -> None:
    """Вернуть аварийный claim только после ручной сверки внешней платформы."""
    db.init_db()
    if not args.confirmed_not_published:
        raise RuntimeError(
            "Сначала проверьте YouTube/Telegram и добавьте --confirmed-not-published"
        )
    if not db.recover_interrupted_claim(args.id):
        raise RuntimeError(f"Ролик #{args.id} не находится в статусе publishing")
    print(f"Ролик #{args.id} возвращён в pending")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Проверить локальные зависимости без публикации и обработки видео."""
    import imageio_ffmpeg  # type: ignore[import-untyped]
    import requests

    from settings import (
        CONFIG,
        INPUT_DIR,
        PENDING_DIR,
        POSTED_DIR,
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHANNEL_ID,
        YOUTUBE_CLIENT_SECRETS_PATH,
        YOUTUBE_TOKEN_PATH,
    )

    checks: list[tuple[str, bool, str]] = []

    ffmpeg_path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    checks.append(("ffmpeg", ffmpeg_path.is_file(), str(ffmpeg_path)))

    host = CONFIG["highlight"]["ollama_host"].rstrip("/")
    model = CONFIG["highlight"]["model"]
    try:
        response = requests.get(f"{host}/api/tags", timeout=5)
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("models", [])}
        model_ok = model in names or any(name and name.split(":")[0] == model for name in names)
        detail = f"{host}; модель {model}: {'найдена' if model_ok else 'не найдена'}"
        checks.append(("Ollama", model_ok, detail))
    except (requests.RequestException, ValueError) as exc:
        checks.append(("Ollama", False, f"{host}: {exc}"))

    for directory in (INPUT_DIR, PENDING_DIR, POSTED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    checks.append(("папки", True, "input, pending и posted доступны"))

    telegram_ok = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID)
    checks.append(
        (
            "Telegram",
            telegram_ok,
            "настроен" if telegram_ok else "не настроен (для нарезки не обязателен)",
        )
    )

    youtube_ready = YOUTUBE_CLIENT_SECRETS_PATH.is_file() and YOUTUBE_TOKEN_PATH.is_file()
    if youtube_ready:
        youtube_detail = "OAuth-файлы найдены"
    elif YOUTUBE_CLIENT_SECRETS_PATH.is_file():
        youtube_detail = "client_secrets найден; требуется команда youtube-auth"
    else:
        youtube_detail = f"не настроен; нужен {YOUTUBE_CLIENT_SECRETS_PATH}"
    checks.append(("YouTube", youtube_ready, youtube_detail))

    required_ok = all(ok for name, ok, _ in checks if name not in {"Telegram", "YouTube"})
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else '--'}] {name}: {detail}")
    print("\nПайплайн готов к нарезке." if required_ok else "\nЕсть обязательные проблемы — см. выше.")
    if not required_ok:
        raise RuntimeError("проверка окружения не пройдена")


def cmd_dorama(args: argparse.Namespace) -> None:
    from dorama.pipeline import create_dorama_video

    create_dorama_video(query=args.query, limit=args.limit)


def cmd_episode(args: argparse.Namespace) -> None:
    from dorama.source_pipeline import create_episode_recap

    create_episode_recap(args.video, focus=args.focus)


def cmd_licensed(args: argparse.Namespace) -> None:
    from dorama.licensed_sources import create_licensed_dorama_video

    create_licensed_dorama_video(args.query, focus=args.focus, limit=args.limit)


def cmd_youtube_auth(args: argparse.Namespace) -> None:
    from publisher import youtube

    token_path = youtube.authorize()
    print(f"YouTube OAuth настроен. Токен сохранён: {token_path}")


def cmd_youtube_preview(args: argparse.Namespace) -> None:
    import json

    from publisher import youtube

    print(json.dumps(youtube.preview(args.id), ensure_ascii=False, indent=2))


def cmd_youtube_publish(args: argparse.Namespace) -> None:
    from publisher import youtube

    video_id = youtube.publish_next(args.id, args.privacy)
    if video_id is None:
        print("Очередь пуста — загружать нечего")
    else:
        print(f"Загружено на YouTube: https://youtu.be/{video_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Автоматизация нарезки и публикации Reels/Shorts")
    sub = parser.add_subparsers(dest="command", required=True)

    p_clip = sub.add_parser("clip", help="Нарезать длинное видео на клипы")
    p_clip.add_argument("video", help="Путь к видеофайлу (обычно из input/)")
    p_clip.set_defaults(func=cmd_clip)

    p_publish = sub.add_parser("publish", help="Опубликовать один клип из очереди сейчас же")
    p_publish.set_defaults(func=cmd_publish)

    p_status = sub.add_parser("status", help="Показать состояние очереди")
    p_status.set_defaults(func=cmd_status)

    p_reject = sub.add_parser("reject", help="Отклонить ролик из очереди проверки")
    p_reject.add_argument("id", type=int, help="ID ролика из команды status")
    p_reject.set_defaults(func=cmd_reject)

    p_recover = sub.add_parser(
        "recover",
        help="Вернуть зависший publishing после проверки внешней платформы",
    )
    p_recover.add_argument("id", type=int, help="ID ролика из команды status")
    p_recover.add_argument(
        "--confirmed-not-published",
        action="store_true",
        help="подтверждение, что видео/сообщение не появилось на платформе",
    )
    p_recover.set_defaults(func=cmd_recover)

    p_doctor = sub.add_parser("doctor", help="Проверить ffmpeg, Ollama, модель и настройки")
    p_doctor.set_defaults(func=cmd_doctor)

    p_dorama = sub.add_parser("dorama", help="Создать оригинальный ролик о трендах китайских дорам")
    p_dorama.add_argument("--query", help="Поисковая тема; по умолчанию берётся из config.yaml")
    p_dorama.add_argument("--limit", type=int, help="Количество тренд-сигналов YouTube")
    p_dorama.set_defaults(func=cmd_dorama)

    p_episode = sub.add_parser(
        "episode",
        help="Сделать пятиминутный пересказ из своей или лицензированной серии",
    )
    p_episode.add_argument("video", help="Путь к серии или трейлеру")
    p_episode.add_argument("--focus", default="", help="Что подчеркнуть в пересказе")
    p_episode.set_defaults(func=cmd_episode)

    p_licensed = sub.add_parser(
        "licensed",
        help="Найти Creative Commons видео и сделать пятиминутный выпуск",
    )
    p_licensed.add_argument("--query", default="китайские дорамы", help="Тема поиска")
    p_licensed.add_argument("--focus", default="", help="Что подчеркнуть в пересказе")
    p_licensed.add_argument("--limit", type=int, default=10, help="Сколько результатов проверить")
    p_licensed.set_defaults(func=cmd_licensed)

    p_yt_auth = sub.add_parser("youtube-auth", help="Один раз подключить YouTube через OAuth")
    p_yt_auth.set_defaults(func=cmd_youtube_auth)

    p_yt_preview = sub.add_parser("youtube-preview", help="Показать метаданные без загрузки")
    p_yt_preview.add_argument("id", type=int, help="ID ролика из команды status")
    p_yt_preview.set_defaults(func=cmd_youtube_preview)

    p_yt_publish = sub.add_parser("youtube-publish", help="Загрузить подтверждённый ролик на YouTube")
    p_yt_publish.add_argument("id", type=int, help="ID ролика из команды status")
    p_yt_publish.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p_yt_publish.set_defaults(func=cmd_youtube_publish)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:  # noqa: BLE001 — человекочитаемая граница CLI
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
