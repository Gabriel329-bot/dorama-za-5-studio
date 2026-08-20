"""TTL-кэш дорогих системных проверок для часто опрашиваемого dashboard."""
from __future__ import annotations

import subprocess
import time
from threading import Lock

import requests


class HealthService:
    def __init__(self, ttl_seconds: float = 15.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._expires_at = 0.0
        self._cache_key: tuple[str, str] | None = None
        self._value: dict[str, bool] = {"ollama": False, "scheduler": False}

    @staticmethod
    def _scheduler_enabled() -> bool:
        from background_services import scheduler_service

        if scheduler_service.running:
            return True
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", "ContentAutomationScheduler"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _ollama_ready(host: str, model: str) -> bool:
        try:
            response = requests.get(f"{host.rstrip('/')}/api/tags", timeout=(0.5, 1.5))
            response.raise_for_status()
            models = {item.get("name") for item in response.json().get("models", [])}
            return model in models or any(
                name and name.split(":")[0] == model.split(":")[0] for name in models
            )
        except (requests.RequestException, TypeError, ValueError):
            return False

    def snapshot(self, host: str, model: str) -> dict[str, bool]:
        now = time.monotonic()
        key = (host, model)
        with self._lock:
            if key == self._cache_key and now < self._expires_at:
                return dict(self._value)
            value = {
                "ollama": self._ollama_ready(host, model),
                "scheduler": self._scheduler_enabled(),
            }
            self._value = value
            self._cache_key = key
            self._expires_at = time.monotonic() + self._ttl_seconds
            return dict(value)
