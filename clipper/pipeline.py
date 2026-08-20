"""Склейка: видео -> транскрипт -> хайлайты -> вертикальные клипы с субтитрами -> очередь."""
import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path

from clipper.highlight import Highlight, pick_highlights
from clipper.render import render_clip
from clipper.transcribe import transcribe
from job_control import checkpoint
from settings import PENDING_DIR
from storage import db
from storage.files import atomic_write_text


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return slug[:max_len] or "clip"


def _load_highlights(path: Path) -> list[Highlight] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return None
        highlights = [
            Highlight(
                start=float(item["start"]),
                end=float(item["end"]),
                caption=str(item["caption"]),
            )
            for item in payload
            if isinstance(item, dict)
        ]
        return highlights or None
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def process_video(
    video_path: str | Path,
    operation_id: str | None = None,
) -> list[Path]:
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

    work_dir = PENDING_DIR / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = (
        work_dir / f"{operation_id}.highlights.json"
        if operation_id
        else None
    )
    highlights = _load_highlights(checkpoint_path) if checkpoint_path else None
    if highlights is None:
        print(f"[2/3] Отбираю хайлайты ({len(transcript.words)} слов в транскрипте)...")
        checkpoint()
        highlights = pick_highlights(transcript)
        if checkpoint_path and highlights:
            atomic_write_text(
                checkpoint_path,
                json.dumps(
                    [asdict(item) for item in highlights],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    else:
        print(f"[2/3] Продолжаю по сохранённому плану: {len(highlights)} клип(ов)")
    if not highlights:
        print("Модель не нашла подходящих отрывков в этом видео.")
        return []

    print(f"[3/3] Рендерю {len(highlights)} клип(ов)...")
    created: list[Path] = []
    for idx, hl in enumerate(highlights, start=1):
        checkpoint()
        slug = _slugify(hl.caption)
        item_origin = f"{operation_id}:{idx}" if operation_id else None
        existing = (
            db.get_clip_by_origin_job(item_origin)
            if item_origin
            else None
        )
        if existing is not None:
            existing_path = Path(str(existing["file_path"]))
            if existing_path.is_file():
                created.append(existing_path)
                print(f"  ↻ уже готово: {existing_path.name}")
                continue
        suffix = operation_id or uuid.uuid4().hex[:8]
        out_name = f"{source.stem}_{idx}_{slug}_{suffix}.mp4"
        out_path = PENDING_DIR / out_name

        render_clip(str(source), hl, transcript.words, out_path)
        db.add_pending(
            str(out_path),
            hl.caption,
            str(source),
            origin_job_id=item_origin,
        )
        created.append(out_path)
        print(f"  -> {out_name} ({hl.end - hl.start:.0f} сек): {hl.caption}")

    if checkpoint_path:
        checkpoint_path.unlink(missing_ok=True)
    return created
