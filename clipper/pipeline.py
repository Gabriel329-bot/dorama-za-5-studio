"""Склейка: видео -> транскрипт -> хайлайты -> вертикальные клипы с субтитрами -> очередь."""
import re
from pathlib import Path

from settings import PENDING_DIR
from clipper.transcribe import transcribe
from clipper.highlight import pick_highlights
from clipper.render import render_clip
from storage import db
from job_control import checkpoint


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return slug[:max_len] or "clip"


def process_video(video_path: str) -> list[Path]:
    db.init_db()
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Видео не найдено: {source}")
    if source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise ValueError(f"Неподдерживаемый формат видео: {source.suffix or '(без расширения)'}")
    print(f"[1/3] Транскрибирую {source.name}...")
    checkpoint()
    transcript = transcribe(str(source))

    print(f"[2/3] Отбираю хайлайты ({len(transcript.words)} слов в транскрипте)...")
    checkpoint()
    highlights = pick_highlights(transcript)
    if not highlights:
        print("Модель не нашла подходящих отрывков в этом видео.")
        return []

    print(f"[3/3] Рендерю {len(highlights)} клип(ов)...")
    created: list[Path] = []
    for idx, hl in enumerate(highlights, start=1):
        checkpoint()
        slug = _slugify(hl.caption)
        out_name = f"{source.stem}_{idx}_{slug}.mp4"
        out_path = PENDING_DIR / out_name

        render_clip(str(source), hl, transcript.words, out_path)
        db.add_pending(str(out_path), hl.caption, str(source))
        created.append(out_path)
        print(f"  -> {out_name} ({hl.end - hl.start:.0f} сек): {hl.caption}")

    return created
