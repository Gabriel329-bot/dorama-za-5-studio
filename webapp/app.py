"""Локальная панель: генерация, очередь, проверка и публикация роликов."""
from __future__ import annotations

import mimetypes
import re
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, TypeAlias

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config_store import apply_config_snapshot, update_config_file
from settings import (
    CONFIG,
    INPUT_DIR,
    ROOT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHANNEL_ID,
    TELEGRAM_USER_ID,
    YOUTUBE_CLIENT_SECRETS_PATH,
    YOUTUBE_TOKEN_PATH,
)
from storage import db
from storage.files import move_to_unique
from webapp.health import HealthService
from webapp.job_handlers import execute_persistent_job
from webapp.job_store import JobStore
from webapp.jobs import JobManager, JobRecord
from webapp.schemas import (
    CaptionRequest,
    ClipRequest,
    DoramaRequest,
    EpisodeRequest,
    LicensedDoramaRequest,
    PipelineSettingsRequest,
    PublishRequest,
    ScheduleRequest,
)
from webapp.security import RequestBodyLimitMiddleware, local_api_guard, session_payload
from webapp.uploads import UploadTooLargeError, UploadValidationError, save_video_upload

STATIC_DIR = Path(__file__).resolve().parent / "static"
BRAND_DIR = ROOT_DIR / "assets" / "branding"
CONFIG_PATH = ROOT_DIR / "config.yaml"
DEFAULT_MAX_UPLOAD_MB = 2048
ApiResponse: TypeAlias = dict[str, Any]


job_manager = JobManager(
    max_workers=1,
    max_history=100,
    max_log_lines=30,
    store=JobStore(),
    persistent_handler=execute_persistent_job,
)
health_service = HealthService(ttl_seconds=15.0)


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    bot_token = TELEGRAM_BOT_TOKEN
    user_id = TELEGRAM_USER_ID
    stop_bot = None
    if bot_token and user_id:
        from telegram_bot.bot import run_bot_thread
        stop_bot = run_bot_thread(bot_token, user_id)
    recovered = job_manager.start()
    if recovered:
        print(f"↻ Восстановлено задач после перезапуска: {recovered}")
    try:
        yield
    finally:
        if stop_bot is not None:
            stop_bot()
        job_manager.shutdown()


app = FastAPI(
    title="Дорама за 5 — Studio",
    description="Локальная панель генерации и публикации оригинальных роликов.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)
app.middleware("http")(local_api_guard)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_upload_bytes=DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
)

# Совместимые read-only aliases для локальных тестов и старых расширений.
jobs = job_manager.jobs
job_controls = job_manager.controls
jobs_lock = job_manager.lock
config_lock = Lock()


def _submit_job(
    kind: str, title: str, runner: Callable[[], object]
) -> JobRecord:
    return job_manager.submit(kind, title, runner)


def _submit_persistent_job(
    kind: str,
    title: str,
    payload: Mapping[str, object],
    *,
    resumable: bool = True,
) -> JobRecord:
    return job_manager.submit_persistent(
        kind,
        title,
        payload,
        resumable=resumable,
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> ApiResponse:
    try:
        return job_manager.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Задача не найдена") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


def _serialize_clip(row: sqlite3.Row) -> ApiResponse:
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
    now = datetime.now(timezone.utc).astimezone()
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


def _update_youtube_config(payload: ScheduleRequest) -> None:
    times = [value.strip() for value in payload.post_times]
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times):
        raise HTTPException(422, "Время должно быть в формате ЧЧ:ММ")

    try:
        with config_lock:
            updated = update_config_file(
                CONFIG_PATH,
                {
                    ("publishing", "youtube", "enabled"): payload.enabled,
                    ("publishing", "youtube", "post_times"): times,
                    ("publishing", "youtube", "privacy_status"): payload.privacy_status,
                },
            )
            apply_config_snapshot(CONFIG, updated)
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(500, f"Не удалось сохранить настройки YouTube: {exc}") from exc


@app.get("/api/dashboard")
def dashboard() -> ApiResponse:
    counts = db.counts_by_status()
    yt = CONFIG["publishing"]["youtube"]
    recent_jobs = job_manager.recent(limit=10)
    health = health_service.snapshot(
        str(CONFIG["highlight"]["ollama_host"]), str(CONFIG["highlight"]["model"])
    )
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
            **health,
            "local_only": True,
        },
        "clips": [_serialize_clip(row) for row in db.recent(limit=40)],
        "jobs": recent_jobs,
    }


@app.get("/api/session")
def api_session() -> dict[str, str]:
    return session_payload()


@app.post("/api/jobs/dorama", status_code=202)
def create_dorama(payload: DoramaRequest) -> JobRecord:
    return _submit_persistent_job(
        "dorama",
        f"Дорама: {payload.query}",
        {"query": payload.query, "limit": payload.limit},
    )


@app.post("/api/jobs/licensed-dorama", status_code=202)
def create_licensed_dorama(payload: LicensedDoramaRequest) -> JobRecord:
    return _submit_persistent_job(
        "licensed-dorama",
        f"CC-поиск: {payload.query}",
        {
            "query": payload.query,
            "focus": payload.focus.strip(),
            "limit": payload.limit,
        },
    )


@app.post("/api/uploads")
def upload_video(file: Annotated[UploadFile, File(...)]) -> ApiResponse:
    max_upload_mb = max(
        1,
        min(int(CONFIG.get("web", {}).get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB)), DEFAULT_MAX_UPLOAD_MB),
    )
    try:
        destination, size = save_video_upload(
            file.file,
            file.filename or "video",
            INPUT_DIR,
            max_upload_mb * 1024 * 1024,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except UploadValidationError as exc:
        raise HTTPException(415, str(exc)) from exc
    return {"filename": destination.name, "size_mb": round(size / 1024 / 1024, 1)}


@app.post("/api/jobs/clip", status_code=202)
def create_clips(payload: ClipRequest) -> JobRecord:
    source = (INPUT_DIR / Path(payload.filename).name).resolve()
    if source.parent != INPUT_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "Загруженный файл не найден")
    return _submit_persistent_job(
        "clip",
        f"Нарезка: {source.name}",
        {"filename": source.name},
    )


@app.post("/api/jobs/episode", status_code=202)
def create_episode(payload: EpisodeRequest) -> JobRecord:
    if CONFIG["episode"].get("require_rights_confirmation", True) and not payload.rights_confirmed:
        raise HTTPException(422, "Подтвердите права на загруженный материал")
    source = (INPUT_DIR / Path(payload.filename).name).resolve()
    if source.parent != INPUT_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "Загруженный файл не найден")
    return _submit_persistent_job(
        "episode",
        f"Пересказ: {source.name}",
        {
            "filename": source.name,
            "focus": payload.focus.strip(),
        },
    )


@app.post("/api/clips/{clip_id}/publish/youtube", status_code=202)
def publish_youtube(clip_id: int, payload: PublishRequest) -> JobRecord:
    clip = db.get_clip(clip_id)
    if clip is None or clip["status"] != "pending":
        raise HTTPException(409, "В очереди нет такого ролика")
    return _submit_persistent_job(
        "youtube",
        f"YouTube: ролик #{clip_id}",
        {"clip_id": clip_id, "privacy": payload.privacy},
        resumable=False,
    )


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: int) -> ApiResponse:
    from settings import REJECTED_DIR

    clip = db.claim_pending("reject", clip_id=clip_id)
    if clip is None:
        raise HTTPException(409, "В очереди нет такого ролика")
    claim_token = str(clip["claim_token"])
    source = Path(clip["file_path"])
    destination = source
    moved = False
    try:
        if source.is_file():
            destination = move_to_unique(source, REJECTED_DIR)
            moved = True
        db.mark_rejected_claimed(clip_id, claim_token, str(destination))
    except BaseException as exc:
        if moved and destination.is_file() and not source.exists():
            destination.replace(source)
            moved = False
        if not moved:
            db.release_claim(clip_id, claim_token, str(exc))
        raise
    return {"ok": True}


@app.patch("/api/clips/{clip_id}")
def edit_caption(clip_id: int, payload: CaptionRequest) -> ApiResponse:
    clip = db.get_clip(clip_id)
    if clip is None or clip["status"] != "pending":
        raise HTTPException(409, "Можно редактировать только ролик в очереди")
    if not db.update_caption(clip_id, payload.caption.strip()):
        raise HTTPException(409, "Ролик уже обрабатывается другой задачей")
    return {"ok": True}


@app.get("/api/clips/{clip_id}/video")
def clip_video(clip_id: int) -> FileResponse:
    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "Ролик не найден")
    path = Path(clip["file_path"])
    if not path.is_file():
        raise HTTPException(404, "Файл ролика не найден")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.post("/api/settings/youtube")
def save_youtube_settings(payload: ScheduleRequest) -> ApiResponse:
    _update_youtube_config(payload)
    return {"ok": True, "next_publish": _next_slot(payload.post_times) if payload.enabled else None}


@app.get("/api/settings/pipeline")
def get_pipeline_settings() -> ApiResponse:
    return {
        "whisper_model": CONFIG["whisper"]["model"],
        "whisper_device": CONFIG["whisper"]["device"],
        "ollama_model": CONFIG["highlight"]["model"],
        "search_sources": CONFIG["dorama"].get("search_sources", []),
        "voice": CONFIG["dorama"]["voice"],
        "rate": CONFIG["dorama"]["rate"],
        "pitch": CONFIG["dorama"]["pitch"],
        "target_duration_seconds": CONFIG["dorama"]["target_duration_seconds"],
        "scene_count": CONFIG["episode"]["scene_count"],
        "original_audio_volume": CONFIG["episode"]["original_audio_volume"],
        "target_script_words": CONFIG["dorama"]["target_script_words"],
        "require_review": CONFIG["dorama"]["require_review"],
    }


@app.post("/api/settings/pipeline")
def save_pipeline_settings(payload: PipelineSettingsRequest) -> ApiResponse:
    updates: dict[tuple[str, ...], object] = {
        ("whisper", "model"): payload.whisper_model,
        ("whisper", "device"): payload.whisper_device,
        ("highlight", "model"): payload.ollama_model,
        ("dorama", "search_sources"): list(payload.search_sources),
        ("dorama", "voice"): payload.voice,
        ("dorama", "rate"): payload.rate,
        ("dorama", "pitch"): payload.pitch,
        ("dorama", "target_duration_seconds"): payload.target_duration_seconds,
        ("dorama", "target_script_words"): payload.target_script_words,
        ("dorama", "require_review"): payload.require_review,
        ("episode", "target_duration_seconds"): payload.target_duration_seconds,
        ("episode", "target_narration_words"): payload.target_script_words,
        ("episode", "scene_count"): payload.scene_count,
        ("episode", "original_audio_volume"): payload.original_audio_volume,
    }
    try:
        with config_lock:
            updated = update_config_file(CONFIG_PATH, updates)
            apply_config_snapshot(CONFIG, updated)
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(500, f"Не удалось сохранить настройки пайплайна: {exc}") from exc
    return {"ok": True}


@app.post("/api/jobs/scheduler", status_code=202)
def run_scheduler_now() -> JobRecord:
    return _submit_persistent_job(
        "scheduler",
        "Проверка расписания",
        {},
        resumable=False,
    )


@app.post("/api/jobs/doctor", status_code=202)
def run_doctor() -> JobRecord:
    return _submit_persistent_job(
        "doctor",
        "Проверка системы",
        {},
    )


app.mount("/brand", StaticFiles(directory=BRAND_DIR), name="brand")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
