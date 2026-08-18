"""Локальная панель: генерация, очередь, проверка и публикация роликов."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Callable
import json
import re
import shutil
import subprocess
import uuid

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from settings import (
    CONFIG,
    INPUT_DIR,
    ROOT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHANNEL_ID,
    YOUTUBE_CLIENT_SECRETS_PATH,
    YOUTUBE_TOKEN_PATH,
)
from storage import db
from job_control import JobCancelled, cancellation_scope


STATIC_DIR = Path(__file__).resolve().parent / "static"
BRAND_DIR = ROOT_DIR / "assets" / "branding"
CONFIG_PATH = ROOT_DIR / "config.yaml"
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

app = FastAPI(
    title="Дорама за 5 — Studio",
    description="Локальная панель генерации и публикации оригинальных роликов.",
    version="1.0.0",
)

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dorama-studio")
jobs: dict[str, dict] = {}
job_controls: dict[str, tuple[Event, Future | None]] = {}
jobs_lock = Lock()


class DoramaRequest(BaseModel):
    query: str = Field(min_length=3, max_length=180)
    limit: int = Field(default=10, ge=3, le=40)


class LicensedDoramaRequest(BaseModel):
    query: str = Field(min_length=3, max_length=180)
    focus: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=3, le=40)


class ClipRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)


class EpisodeRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    focus: str = Field(default="", max_length=300)
    rights_confirmed: bool


class PublishRequest(BaseModel):
    privacy: str = Field(default="private", pattern="^(private|unlisted|public)$")


class CaptionRequest(BaseModel):
    caption: str = Field(min_length=3, max_length=5000)


class ScheduleRequest(BaseModel):
    enabled: bool
    post_times: list[str] = Field(min_length=1, max_length=6)
    privacy_status: str = Field(pattern="^(private|unlisted|public)$")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobWriter:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._append(line.strip())
        return len(value)

    def flush(self) -> None:
        if self.buffer.strip():
            self._append(self.buffer.strip())
            self.buffer = ""

    def _append(self, line: str) -> None:
        if not line:
            return
        progress = None
        match = re.match(r"\[(\d+)/(\d+)]", line)
        if match:
            progress = 12 + round((int(match.group(1)) - 1) / int(match.group(2)) * 72)
        if line.startswith("Готово") or "Опубликовано" in line:
            progress = 96
        with jobs_lock:
            job = jobs.get(self.job_id)
            if not job:
                return
            job["logs"] = [*job["logs"], line][-30:]
            if job["status"] != "cancelling":
                job["message"] = line
            if progress is not None:
                job["progress"] = progress


def _submit_job(kind: str, title: str, runner: Callable[[], object]) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "kind": kind,
        "title": title,
        "status": "queued",
        "progress": 4,
        "message": "Задача добавлена в очередь",
        "logs": [],
        "created_at": _now_iso(),
        "finished_at": None,
        "result": None,
    }
    with jobs_lock:
        jobs[job_id] = job
        cancel_event = Event()
        job_controls[job_id] = (cancel_event, None)

    def wrapped() -> None:
        writer = JobWriter(job_id)
        with jobs_lock:
            if cancel_event.is_set():
                jobs[job_id].update(status="cancelled", message="Отменено", finished_at=_now_iso())
                return
            jobs[job_id].update(status="running", progress=8, message="Запускаю обработку")
        try:
            with cancellation_scope(cancel_event):
                with redirect_stdout(writer), redirect_stderr(writer):
                    result = runner()
            writer.flush()
        except JobCancelled:
            writer.flush()
            with jobs_lock:
                jobs[job_id].update(
                    status="cancelled",
                    message="Отменено пользователем",
                    logs=[*jobs[job_id]["logs"], "Процесс остановлен"][-30:],
                    finished_at=_now_iso(),
                )
            return
        except Exception as exc:  # noqa: BLE001 — ошибка должна попасть в интерфейс
            writer.flush()
            with jobs_lock:
                jobs[job_id].update(
                    status="failed",
                    message=str(exc),
                    logs=[*jobs[job_id]["logs"], f"Ошибка: {exc}"][-30:],
                    finished_at=_now_iso(),
                )
            return
        with jobs_lock:
            jobs[job_id].update(
                status="succeeded",
                progress=100,
                message="Готово",
                result=str(result) if result is not None else None,
                finished_at=_now_iso(),
            )

    future = executor.submit(wrapped)
    with jobs_lock:
        job_controls[job_id] = (cancel_event, future)
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            raise HTTPException(409, "Эта задача уже завершена")
        cancel_event, future = job_controls[job_id]
        cancel_event.set()
        if job["status"] == "queued" and future is not None and future.cancel():
            job.update(status="cancelled", message="Убрано из очереди", finished_at=_now_iso())
        else:
            job.update(status="cancelling", message="Останавливаю выбранный процесс…")
        return {"id": job_id, "status": job["status"]}


def _serialize_clip(row) -> dict:
    file_path = Path(row["file_path"])
    return {
        "id": row["id"],
        "caption": row["caption"],
        "source_video": row["source_video"],
        "status": row["status"],
        "platform": row["platform"],
        "created_at": row["created_at"],
        "posted_at": row["posted_at"],
        "file_exists": file_path.is_file(),
        "filename": file_path.name,
        "size_mb": round(file_path.stat().st_size / 1024 / 1024, 1) if file_path.is_file() else 0,
    }


def _next_slot(post_times: list[str]) -> str | None:
    now = datetime.now()
    candidates: list[datetime] = []
    for raw in post_times:
        hour, minute = map(int, raw.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates).astimezone().isoformat(timespec="minutes")


def _scheduler_enabled() -> bool:
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "ContentAutomationScheduler"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ollama_ready() -> bool:
    try:
        response = requests.get(f"{CONFIG['highlight']['ollama_host'].rstrip('/')}/api/tags", timeout=1.5)
        response.raise_for_status()
        models = {item.get("name") for item in response.json().get("models", [])}
        wanted = CONFIG["highlight"]["model"]
        return wanted in models or any(name and name.split(":")[0] == wanted.split(":")[0] for name in models)
    except (requests.RequestException, ValueError):
        return False


def _update_youtube_config(payload: ScheduleRequest) -> None:
    times = [value.strip() for value in payload.post_times]
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times):
        raise HTTPException(422, "Время должно быть в формате ЧЧ:ММ")

    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    in_publishing = False
    in_youtube = False
    replaced = {"enabled": False, "post_times": False, "privacy_status": False}
    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_publishing = stripped == "publishing:"
            in_youtube = False
        elif in_publishing and indent == 2:
            in_youtube = stripped == "youtube:"
        elif in_youtube and indent == 4:
            key = stripped.split(":", 1)[0]
            comment = ""
            if "#" in line:
                comment = "  #" + line.split("#", 1)[1]
            if key == "enabled":
                lines[index] = f"    enabled: {str(payload.enabled).lower()}{comment}"
                replaced[key] = True
            elif key == "post_times":
                lines[index] = f"    post_times: {json.dumps(times, ensure_ascii=False)}{comment}"
                replaced[key] = True
            elif key == "privacy_status":
                lines[index] = f"    privacy_status: {payload.privacy_status}{comment}"
                replaced[key] = True
    if not all(replaced.values()):
        raise HTTPException(500, "Не удалось найти настройки YouTube в config.yaml")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    CONFIG["publishing"]["youtube"].update(
        enabled=payload.enabled,
        post_times=times,
        privacy_status=payload.privacy_status,
    )


@app.get("/api/dashboard")
def dashboard() -> dict:
    db.init_db()
    counts = db.counts_by_status()
    yt = CONFIG["publishing"]["youtube"]
    with jobs_lock:
        recent_jobs = sorted(jobs.values(), key=lambda item: item["created_at"], reverse=True)[:10]
    return {
        "brand": {"name": "Дорама за 5", "handle": "@dorama_za_5"},
        "stats": {
            "pending": counts.get("pending", 0),
            "posted": counts.get("posted", 0),
            "rejected": counts.get("rejected", 0),
            "next_publish": _next_slot(yt.get("post_times", [])) if yt.get("enabled") else None,
        },
        "pipeline": {
            "duration_seconds": CONFIG["dorama"]["target_duration_seconds"],
            "voice": CONFIG["dorama"]["voice"],
            "voice_preset": CONFIG["dorama"]["voice_preset"],
            "hashtags": CONFIG["dorama"]["base_hashtags"],
            "query": CONFIG["dorama"]["search_query"],
            "search_results": CONFIG["dorama"]["search_results"],
            "search_sources": CONFIG["dorama"].get("search_sources", []),
            "require_review": CONFIG["dorama"]["require_review"],
            "licensed_search": True,
        },
        "platforms": {
            "youtube": {
                "connected": YOUTUBE_CLIENT_SECRETS_PATH.is_file() and YOUTUBE_TOKEN_PATH.is_file(),
                "enabled": bool(yt.get("enabled")),
                "privacy": yt.get("privacy_status", "private"),
                "post_times": yt.get("post_times", []),
            },
            "telegram": {
                "connected": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID),
                "enabled": bool(CONFIG["publishing"]["telegram"].get("enabled")),
            },
            "tiktok": {"connected": False, "enabled": False, "reason": "Нужен доступ к Content Posting API"},
        },
        "system": {
            "ollama": _ollama_ready(),
            "scheduler": _scheduler_enabled(),
            "local_only": True,
        },
        "clips": [_serialize_clip(row) for row in db.recent(limit=40)],
        "jobs": recent_jobs,
    }


@app.post("/api/jobs/dorama", status_code=202)
def create_dorama(payload: DoramaRequest) -> dict:
    from dorama.pipeline import create_dorama_video

    return _submit_job(
        "dorama",
        f"Дорама: {payload.query}",
        lambda: create_dorama_video(query=payload.query, limit=payload.limit),
    )


@app.post("/api/jobs/licensed-dorama", status_code=202)
def create_licensed_dorama(payload: LicensedDoramaRequest) -> dict:
    from dorama.licensed_sources import create_licensed_dorama_video

    return _submit_job(
        "licensed-dorama",
        f"CC-поиск: {payload.query}",
        lambda: create_licensed_dorama_video(
            query=payload.query,
            focus=payload.focus.strip(),
            limit=payload.limit,
        ),
    )


@app.post("/api/uploads")
def upload_video(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(415, "Поддерживаются MP4, MOV, MKV, WEBM и AVI")
    safe_stem = re.sub(r"[^\wа-яА-ЯёЁ-]+", "_", Path(file.filename or "video").stem).strip("_")[:80]
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    destination = INPUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_stem or 'video'}{suffix}"
    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    return {"filename": destination.name, "size_mb": round(destination.stat().st_size / 1024 / 1024, 1)}


@app.post("/api/jobs/clip", status_code=202)
def create_clips(payload: ClipRequest) -> dict:
    from clipper.pipeline import process_video

    source = (INPUT_DIR / Path(payload.filename).name).resolve()
    if source.parent != INPUT_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "Загруженный файл не найден")
    return _submit_job("clip", f"Нарезка: {source.name}", lambda: process_video(source))


@app.post("/api/jobs/episode", status_code=202)
def create_episode(payload: EpisodeRequest) -> dict:
    if CONFIG["episode"].get("require_rights_confirmation", True) and not payload.rights_confirmed:
        raise HTTPException(422, "Подтвердите права на загруженный материал")
    source = (INPUT_DIR / Path(payload.filename).name).resolve()
    if source.parent != INPUT_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "Загруженный файл не найден")
    from dorama.source_pipeline import create_episode_recap

    return _submit_job(
        "episode",
        f"Пересказ: {source.name}",
        lambda: create_episode_recap(source, focus=payload.focus.strip()),
    )


@app.post("/api/clips/{clip_id}/publish/youtube", status_code=202)
def publish_youtube(clip_id: int, payload: PublishRequest) -> dict:
    clip = db.get_clip(clip_id)
    if clip is None or clip["status"] != "pending":
        raise HTTPException(409, "В очереди нет такого ролика")
    from publisher import youtube

    return _submit_job(
        "youtube",
        f"YouTube: ролик #{clip_id}",
        lambda: youtube.publish_next(clip_id=clip_id, privacy_status=payload.privacy),
    )


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: int) -> dict:
    from settings import REJECTED_DIR

    clip = db.get_clip(clip_id)
    if clip is None or clip["status"] != "pending":
        raise HTTPException(409, "В очереди нет такого ролика")
    source = Path(clip["file_path"])
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    destination = REJECTED_DIR / source.name
    if source.is_file():
        shutil.move(str(source), str(destination))
    db.mark_rejected(clip_id, str(destination))
    return {"ok": True}


@app.patch("/api/clips/{clip_id}")
def edit_caption(clip_id: int, payload: CaptionRequest) -> dict:
    clip = db.get_clip(clip_id)
    if clip is None or clip["status"] != "pending":
        raise HTTPException(409, "Можно редактировать только ролик в очереди")
    db.update_caption(clip_id, payload.caption.strip())
    return {"ok": True}


@app.get("/api/clips/{clip_id}/video")
def clip_video(clip_id: int):
    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "Ролик не найден")
    path = Path(clip["file_path"])
    if not path.is_file():
        raise HTTPException(404, "Файл ролика не найден")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/settings/youtube")
def save_youtube_settings(payload: ScheduleRequest) -> dict:
    _update_youtube_config(payload)
    return {"ok": True, "next_publish": _next_slot(payload.post_times) if payload.enabled else None}


@app.post("/api/jobs/scheduler", status_code=202)
def run_scheduler_now() -> dict:
    from scheduler import check_and_publish

    return _submit_job("scheduler", "Проверка расписания", check_and_publish)


@app.post("/api/jobs/doctor", status_code=202)
def run_doctor() -> dict:
    import argparse
    from cli import cmd_doctor

    return _submit_job("doctor", "Проверка системы", lambda: cmd_doctor(argparse.Namespace()))


app.mount("/brand", StaticFiles(directory=BRAND_DIR), name="brand")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
