"""Управляемые фоновые сервисы, живущие вместе с локальным API."""
from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Lock, Thread

from scheduler import check_and_publish
from settings import CONFIG

log = logging.getLogger("dorama-services")


class SchedulerService:
    """Периодически проверять расписание без Windows Task Scheduler."""

    def __init__(
        self,
        checker: Callable[[], None] = check_and_publish,
        *,
        interval_seconds: float = 60.0,
        initial_delay_seconds: float = 5.0,
    ) -> None:
        self._checker = checker
        self._interval_seconds = max(0.05, interval_seconds)
        self._initial_delay_seconds = max(0.0, initial_delay_seconds)
        self._stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event = Event()
            self._thread = Thread(
                target=self._run,
                daemon=True,
                name="publication-scheduler",
            )
            self._thread.start()
        log.info("Встроенный планировщик запущен")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
        stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
        log.info("Встроенный планировщик остановлен")

    def _run(self) -> None:
        if self._stop_event.wait(self._initial_delay_seconds):
            return
        while not self._stop_event.is_set():
            publishing = CONFIG.get("publishing", {})
            enabled = any(
                bool((publishing.get(platform) or {}).get("enabled"))
                for platform in ("youtube", "telegram")
            )
            review_required = bool(
                CONFIG.get("dorama", {}).get("require_review", True)
            )
            if enabled and not review_required:
                try:
                    self._checker()
                except Exception:
                    log.exception("Ошибка проверки расписания")
            if self._stop_event.wait(self._interval_seconds):
                return


scheduler_service = SchedulerService()
