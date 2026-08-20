"""Тренды -> оригинальный сценарий -> озвучка -> ролик -> очередь проверки."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dorama.discover import search_all_sources
from dorama.render import create_cover, render_video
from dorama.script import DoramaScript, create_script
from dorama.speech import synthesize
from job_control import checkpoint
from settings import CONFIG, PENDING_DIR
from storage import db
from storage.files import atomic_write_text


def _slug(text: str) -> str:
    value = re.sub(r"[^\wа-яА-ЯёЁ-]+", "_", text, flags=re.UNICODE).strip("_")
    return value[:45] or "dorama_radar"


def _artifact_id() -> str:
    now = datetime.now(timezone.utc).astimezone()
    return f"{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def _load_script_checkpoint(path: Path) -> DoramaScript | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DoramaScript(
            title=str(data["title"]),
            narration=str(data["narration"]),
            caption=str(data["caption"]),
            hashtags=[str(item) for item in data["hashtags"]],
            sources=[str(item) for item in data["sources"]],
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def create_dorama_video(
    query: str | None = None,
    limit: int | None = None,
    operation_id: str | None = None,
) -> Path:
    cfg = CONFIG["dorama"]
    selected_query = (query or str(cfg["search_query"])).strip()
    selected_limit = int(limit or cfg["search_results"])
    if not 3 <= len(selected_query) <= 180:
        raise ValueError("Поисковый запрос должен содержать от 3 до 180 символов")
    if not 3 <= selected_limit <= 40:
        raise ValueError("Количество результатов должно быть от 3 до 40")
    work_root = PENDING_DIR / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    artifact_id = operation_id or _artifact_id()
    checkpoint_path = (
        work_root / f"{operation_id}.script.json"
        if operation_id
        else None
    )
    script = (
        _load_script_checkpoint(checkpoint_path)
        if checkpoint_path
        else None
    )
    if script is None:
        print(f"[1/4] Ищу тренды на нескольких видеоплощадках: {selected_query}")
        checkpoint()
        trends = search_all_sources(selected_query, selected_limit)
        if not trends:
            raise RuntimeError("Подключённые источники не вернули результатов")

        print(f"[2/4] Создаю оригинальный сценарий по {len(trends)} тренд-сигналам...")
        checkpoint()
        script = create_script(trends, selected_query)
        if checkpoint_path:
            atomic_write_text(
                checkpoint_path,
                json.dumps(asdict(script), ensure_ascii=False, indent=2),
            )
    else:
        print("[1/4] ↻ Использую сохранённый сценарий после перезапуска")
        print("[2/4] Сценарий восстановлен")
    output_path = PENDING_DIR / f"{artifact_id}_{_slug(script.title)}.mp4"
    audit_path = output_path.with_suffix(".script.txt")

    try:
        with tempfile.TemporaryDirectory(prefix=f"radar_{artifact_id}_", dir=work_root) as temporary:
            work_dir = Path(temporary)
            audio_suffix = ".wav" if cfg.get("tts_provider") == "chatterbox" else ".mp3"
            audio_path = work_dir / f"voice{audio_suffix}"
            cover_path = work_dir / "cover.jpg"

            print(f"[3/4] Озвучиваю: {script.title}")
            checkpoint()
            asyncio.run(synthesize(script.narration, audio_path, cfg["voice"], cfg["rate"]))
            create_cover(script.title, cover_path)

            print("[4/4] Собираю вертикальный ролик с субтитрами...")
            checkpoint()
            render_video(
                cover_path,
                audio_path,
                script.narration,
                output_path,
                target_duration=float(cfg["target_duration_seconds"]),
            )

        hashtags = " ".join(script.hashtags)
        caption = f"{script.caption}\n\n{hashtags}".strip()
        source_ref = "dorama-trends:" + "|".join(script.sources)
        checkpoint()
        atomic_write_text(
            audit_path,
            f"{script.title}\n\n{script.narration}\n\n{caption}"
            f"\n\nИсточники сигналов:\n" + "\n".join(script.sources),
        )
        db.init_db()
        db.add_pending(
            str(output_path),
            caption,
            source_ref,
            origin_job_id=operation_id,
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        raise

    if checkpoint_path:
        checkpoint_path.unlink(missing_ok=True)
    print(f"Готово: {output_path.name}")
    print("Ролик добавлен в очередь проверки и автоматически не опубликован.")
    return Path(output_path)
