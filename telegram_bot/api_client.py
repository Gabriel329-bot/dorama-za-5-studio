"""Синхронный клиент Telegram-интерфейса к общему локальному FastAPI."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, TypeAlias, cast
from urllib.parse import quote

import httpx

BASE = "http://127.0.0.1:8765"
_TIMEOUT = 12.0
_MEDIA_TIMEOUT = 900.0
_csrf_token = ""
JsonDict: TypeAlias = dict[str, Any]


def _client() -> httpx.Client:
    """Локальный API никогда не должен ходить через системный HTTP(S)-proxy."""
    return httpx.Client(
        base_url=BASE,
        timeout=httpx.Timeout(_TIMEOUT, connect=2.0),
        trust_env=False,
    )


def _json_object(response: httpx.Response) -> JsonDict:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("Локальный API вернул JSON не в виде объекта")
    return cast(JsonDict, payload)


def _get(path: str) -> JsonDict:
    with _client() as client:
        return _json_object(client.get(path))


def _mutate(method: str, path: str, json: JsonDict | None = None) -> JsonDict:
    global _csrf_token
    if not _csrf_token:
        _csrf_token = str(_get("/api/session")["csrf_token"])
    with _client() as client:
        response = client.request(
            method,
            path,
            json=json,
            headers={"X-Dorama-CSRF": _csrf_token},
        )
        if response.status_code == 403:
            _csrf_token = str(_get("/api/session")["csrf_token"])
            response = client.request(
                method,
                path,
                json=json,
                headers={"X-Dorama-CSRF": _csrf_token},
            )
        return _json_object(response)


def _post(path: str, json: JsonDict | None = None) -> JsonDict:
    return _mutate("POST", path, json)


def _patch(path: str, json: JsonDict) -> JsonDict:
    return _mutate("PATCH", path, json)


def _delete(path: str) -> JsonDict:
    return _mutate("DELETE", path)


def dashboard() -> JsonDict:
    return _get("/api/dashboard")


def pipeline_settings() -> JsonDict:
    return _get("/api/settings/pipeline")


def start_dorama(query: str, limit: int = 10) -> JsonDict:
    return _post("/api/jobs/dorama", {"query": query, "limit": limit})


def start_licensed(query: str, focus: str = "", limit: int = 10) -> JsonDict:
    return _post("/api/jobs/licensed-dorama", {"query": query, "focus": focus, "limit": limit})


def start_episode(
    filename: str,
    focus: str = "",
    *,
    mode: str = "recap",
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> JsonDict:
    payload: JsonDict = {
        "filename": filename,
        "focus": focus,
        "rights_confirmed": True,
        "mode": mode,
        "start_seconds": start_seconds,
    }
    if end_seconds is not None:
        payload["end_seconds"] = end_seconds
    return _post("/api/jobs/episode", payload)


def start_clip(filename: str) -> JsonDict:
    return _post("/api/jobs/clip", {"filename": filename})


def cancel_job(job_id: str) -> JsonDict:
    return _post(f"/api/jobs/{job_id}/cancel")


def clear_job_history() -> JsonDict:
    return _delete("/api/jobs/history")


def run_scheduler() -> JsonDict:
    return _post("/api/jobs/scheduler")


def run_doctor() -> JsonDict:
    return _post("/api/jobs/doctor")


def reject_clip(clip_id: int) -> JsonDict:
    return _post(f"/api/clips/{clip_id}/reject")


def delete_clip(clip_id: int) -> JsonDict:
    return _delete(f"/api/clips/{clip_id}")


def edit_caption(clip_id: int, caption: str) -> JsonDict:
    return _patch(f"/api/clips/{clip_id}", {"caption": caption})


def publish_youtube(clip_id: int, privacy: str = "private") -> JsonDict:
    return _post(f"/api/clips/{clip_id}/publish/youtube", {"privacy": privacy})


def clips(status: str | None = None) -> list[JsonDict]:
    data = dashboard()
    raw_clips = data.get("clips", [])
    if not isinstance(raw_clips, list):
        raise TypeError("Локальный API вернул некорректный список роликов")
    result = [cast(JsonDict, clip) for clip in raw_clips if isinstance(clip, dict)]
    return result if status is None else [clip for clip in result if clip.get("status") == status]


def pending_clips() -> list[JsonDict]:
    """Совместимость со старым меню и внешними интеграциями."""
    return clips("pending")


def clip(clip_id: int) -> JsonDict:
    return _get(f"/api/clips/{clip_id}")


def upload_video(path: Path) -> JsonDict:
    """Передать полученный Telegram-файл в тот же валидатор, что использует web."""
    global _csrf_token
    if not _csrf_token:
        _csrf_token = str(_get("/api/session")["csrf_token"])
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    for attempt in range(2):
        with path.open("rb") as stream, _client() as client:
            response = client.post(
                "/api/uploads",
                files={"file": (path.name, stream, media_type)},
                headers={"X-Dorama-CSRF": _csrf_token},
                timeout=httpx.Timeout(_MEDIA_TIMEOUT, connect=2.0),
            )
        if response.status_code != 403 or attempt:
            return _json_object(response)
        _csrf_token = str(_get("/api/session")["csrf_token"])
    raise RuntimeError("Не удалось открыть защищённую API-сессию")


def upload_metadata(filename: str) -> JsonDict:
    safe_name = quote(Path(filename).name, safe="")
    return _get(f"/api/uploads/{safe_name}")


def download_clip(clip_id: int, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(f"{destination.suffix}.part")
    try:
        with _client() as client, client.stream(
            "GET",
            f"/api/clips/{clip_id}/video",
            timeout=httpx.Timeout(_MEDIA_TIMEOUT, connect=2.0),
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return destination


def save_pipeline_settings(settings: JsonDict) -> JsonDict:
    return _post("/api/settings/pipeline", settings)


def save_youtube_settings(
    *,
    enabled: bool,
    post_times: list[str],
    privacy_status: str,
) -> JsonDict:
    return _post(
        "/api/settings/youtube",
        {
            "enabled": enabled,
            "post_times": post_times,
            "privacy_status": privacy_status,
        },
    )
