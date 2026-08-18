"""Тренды -> оригинальный сценарий -> озвучка -> ролик -> очередь проверки."""
import asyncio
from datetime import datetime
from pathlib import Path
import re

from dorama.discover import search_all_sources
from dorama.render import create_cover, render_video
from dorama.script import create_script
from dorama.speech import synthesize
from settings import CONFIG, PENDING_DIR
from storage import db
from job_control import checkpoint


def _slug(text: str) -> str:
    value = re.sub(r"[^\wа-яА-ЯёЁ-]+", "_", text, flags=re.UNICODE).strip("_")
    return value[:45] or "dorama_radar"


def create_dorama_video(query: str | None = None, limit: int | None = None) -> Path:
    cfg = CONFIG["dorama"]
    query = query or cfg["search_query"]
    limit = limit or cfg["search_results"]
    print(f"[1/4] Ищу тренды на нескольких видеоплощадках: {query}")
    checkpoint()
    trends = search_all_sources(query, limit)
    if not trends:
        raise RuntimeError("Подключённые источники не вернули результатов")

    print(f"[2/4] Создаю оригинальный сценарий по {len(trends)} тренд-сигналам...")
    checkpoint()
    script = create_script(trends, query)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = PENDING_DIR / ".work" / stamp
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_suffix = ".wav" if cfg.get("tts_provider") == "chatterbox" else ".mp3"
    audio_path = work_dir / f"voice{audio_suffix}"
    cover_path = work_dir / "cover.jpg"
    output_path = PENDING_DIR / f"{stamp}_{_slug(script.title)}.mp4"

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
        target_duration=cfg["target_duration_seconds"],
    )
    hashtags = " ".join(script.hashtags)
    caption = f"{script.caption}\n\n{hashtags}".strip()
    source_ref = "dorama-trends:" + "|".join(script.sources)
    db.init_db()
    checkpoint()
    db.add_pending(str(output_path), caption, source_ref)

    (work_dir / "script.txt").write_text(
        f"{script.title}\n\n{script.narration}\n\n{caption}\n\nИсточники сигналов:\n" + "\n".join(script.sources),
        encoding="utf-8",
    )
    print(f"Готово: {output_path.name}")
    print("Ролик добавлен в очередь проверки и автоматически не опубликован.")
    return output_path
