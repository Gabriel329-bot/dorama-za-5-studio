"""Синхронный HTTP-клиент к FastAPI на localhost:8765."""
from __future__ import annotations

from typing import Any, TypeAlias, cast

import httpx

BASE = "http://127.0.0.1:8765"
_TIMEOUT = 12.0
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


def _post(path: str, json: JsonDict | None = None) -> JsonDict:
    global _csrf_token
    if not _csrf_token:
        _csrf_token = _get("/api/session")["csrf_token"]
    with _client() as client:
        response = client.post(
            path,
            json=json,
            headers={"X-Dorama-CSRF": _csrf_token},
        )
        if response.status_code == 403:
            _csrf_token = _get("/api/session")["csrf_token"]
            response = client.post(
                path,
                json=json,
                headers={"X-Dorama-CSRF": _csrf_token},
            )
        return _json_object(response)


def dashboard() -> JsonDict:
    return _get("/api/dashboard")


def pipeline_settings() -> JsonDict:
    return _get("/api/settings/pipeline")


def start_dorama(query: str, limit: int = 10) -> JsonDict:
    return _post("/api/jobs/dorama", {"query": query, "limit": limit})


def start_licensed(query: str, focus: str = "", limit: int = 10) -> JsonDict:
    return _post("/api/jobs/licensed-dorama", {"query": query, "focus": focus, "limit": limit})


def start_episode(filename: str, focus: str = "") -> JsonDict:
    return _post("/api/jobs/episode", {"filename": filename, "focus": focus, "rights_confirmed": True})


def start_clip(filename: str) -> JsonDict:
    return _post("/api/jobs/clip", {"filename": filename})


def cancel_job(job_id: str) -> JsonDict:
    return _post(f"/api/jobs/{job_id}/cancel")


def run_scheduler() -> JsonDict:
    return _post("/api/jobs/scheduler")


def run_doctor() -> JsonDict:
    return _post("/api/jobs/doctor")


def reject_clip(clip_id: int) -> JsonDict:
    return _post(f"/api/clips/{clip_id}/reject")


def publish_youtube(clip_id: int, privacy: str = "private") -> JsonDict:
    return _post(f"/api/clips/{clip_id}/publish/youtube", {"privacy": privacy})


def pending_clips() -> list[JsonDict]:
    data = dashboard()
    clips = data.get("clips", [])
    if not isinstance(clips, list):
        raise TypeError("Локальный API вернул некорректный список роликов")
    return [cast(JsonDict, clip) for clip in clips if isinstance(clip, dict) and clip.get("status") == "pending"]
