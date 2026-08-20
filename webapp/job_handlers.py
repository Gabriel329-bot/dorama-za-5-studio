"""Serializable job dispatch used for automatic restart recovery."""
from __future__ import annotations

import argparse
from pathlib import Path

from settings import INPUT_DIR
from storage import db
from webapp.job_store import JsonDict


def _required_text(payload: JsonDict, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"В сохранённой задаче отсутствует поле {key}")
    return value


def _input_file(payload: JsonDict) -> Path:
    filename = Path(_required_text(payload, "filename")).name
    source = (INPUT_DIR / filename).resolve()
    if source.parent != INPUT_DIR.resolve() or not source.is_file():
        raise FileNotFoundError(f"Исходный файл задачи не найден: {filename}")
    return Path(source)


def _completed_output(job_id: str) -> Path | None:
    row = db.get_clip_by_origin_job(job_id)
    if row is None:
        return None
    path = Path(str(row["file_path"]))
    return path if path.is_file() else None


def execute_persistent_job(
    job_id: str,
    kind: str,
    payload: JsonDict,
) -> object:
    """Dispatch a validated durable payload without retaining lambdas in RAM."""
    if kind in {"dorama", "licensed-dorama", "episode", "literal-translation"}:
        completed = _completed_output(job_id)
        if completed is not None:
            print(f"↻ Использую уже готовый результат: {completed.name}")
            return completed

    if kind == "dorama":
        from dorama.pipeline import create_dorama_video

        return create_dorama_video(
            query=_required_text(payload, "query"),
            limit=int(payload.get("limit") or 10),
            operation_id=job_id,
        )

    if kind == "licensed-dorama":
        from dorama.licensed_sources import create_licensed_dorama_video

        return create_licensed_dorama_video(
            query=_required_text(payload, "query"),
            focus=str(payload.get("focus") or "").strip(),
            limit=int(payload.get("limit") or 10),
            operation_id=job_id,
        )

    if kind == "clip":
        from clipper.pipeline import process_video

        return process_video(_input_file(payload), operation_id=job_id)

    if kind == "episode":
        from dorama.source_pipeline import create_episode_recap

        return create_episode_recap(
            _input_file(payload),
            focus=str(payload.get("focus") or "").strip(),
            operation_id=job_id,
        )

    if kind == "literal-translation":
        from dorama.literal_translation import create_literal_translation

        return create_literal_translation(
            _input_file(payload),
            start_seconds=float(payload.get("start_seconds") or 0.0),
            end_seconds=float(payload["end_seconds"]),
            operation_id=job_id,
        )

    if kind == "doctor":
        from cli import cmd_doctor

        cmd_doctor(argparse.Namespace())
        return None

    if kind == "youtube":
        from publisher import youtube

        return youtube.publish_next(
            clip_id=int(payload["clip_id"]),
            privacy_status=str(payload.get("privacy") or "private"),
        )

    if kind == "scheduler":
        from scheduler import check_and_publish

        check_and_publish()
        return None

    raise ValueError(f"Неизвестный тип сохранённой задачи: {kind}")
