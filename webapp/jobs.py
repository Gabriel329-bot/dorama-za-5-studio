"""Ограниченный по памяти менеджер фоновых задач локальной студии."""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from threading import Event, Lock
from typing import Literal, TypedDict

from job_control import JobCancelled, cancellation_scope

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
JobStatus = Literal[
    "queued", "running", "cancelling", "cancelled", "succeeded", "failed"
]


class JobRecord(TypedDict):
    id: str
    kind: str
    title: str
    status: JobStatus
    progress: int
    message: str
    logs: list[str]
    created_at: str
    finished_at: str | None
    result: str | None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobWriter:
    """Преобразует stdout runner-а в ограниченный лог задачи."""

    _MAX_PENDING_CHARS = 8192

    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self._job_id = job_id
        self._buffer = ""

    def write(self, value: str) -> int:
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._append(line.strip())
        if len(self._buffer) > self._MAX_PENDING_CHARS:
            self._append(self._buffer[: self._MAX_PENDING_CHARS].strip())
            self._buffer = self._buffer[self._MAX_PENDING_CHARS :]
        return len(value)

    def flush(self) -> None:
        if self._buffer.strip():
            self._append(self._buffer.strip())
        self._buffer = ""

    def _append(self, line: str) -> None:
        if not line:
            return
        progress: int | None = None
        match = re.match(r"\[(\d+)/(\d+)]", line)
        if match and int(match.group(2)) > 0:
            progress = 12 + round((int(match.group(1)) - 1) / int(match.group(2)) * 72)
        if line.startswith("Готово") or "Опубликовано" in line:
            progress = 96
        self._manager.append_log(self._job_id, line, progress)


class JobManager:
    def __init__(
        self,
        max_workers: int = 1,
        max_history: int = 100,
        max_log_lines: int = 30,
    ) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="dorama-studio"
        )
        self.jobs: dict[str, JobRecord] = {}
        self.controls: dict[str, tuple[Event, Future[None] | None]] = {}
        self.lock = Lock()
        self.max_history = max(10, max_history)
        self.max_log_lines = max(5, max_log_lines)

    def submit(self, kind: str, title: str, runner: Callable[[], object]) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        job = JobRecord(
            id=job_id,
            kind=kind,
            title=title,
            status="queued",
            progress=4,
            message="Задача добавлена в очередь",
            logs=[],
            created_at=_now_iso(),
            finished_at=None,
            result=None,
        )
        cancel_event = Event()
        with self.lock:
            self.jobs[job_id] = job
            self.controls[job_id] = (cancel_event, None)
            self._prune_locked()

        future = self.executor.submit(self._run, job_id, cancel_event, runner)
        with self.lock:
            if job_id in self.controls:
                self.controls[job_id] = (cancel_event, future)
        return job.copy()

    def _run(self, job_id: str, cancel_event: Event, runner: Callable[[], object]) -> None:
        writer = JobWriter(self, job_id)
        with self.lock:
            if cancel_event.is_set():
                self._finish_locked(job_id, "cancelled", "Отменено")
                return
            job = self.jobs[job_id]
            job["status"] = "running"
            job["progress"] = 8
            job["message"] = "Запускаю обработку"
        try:
            with (
                cancellation_scope(cancel_event),
                redirect_stdout(writer),
                redirect_stderr(writer),
            ):
                result = runner()
            writer.flush()
        except JobCancelled:
            writer.flush()
            with self.lock:
                self._finish_locked(
                    job_id, "cancelled", "Отменено пользователем", "Процесс остановлен"
                )
            return
        except Exception as exc:  # noqa: BLE001 — сообщение нужно показать в UI
            writer.flush()
            with self.lock:
                self._finish_locked(job_id, "failed", str(exc), f"Ошибка: {exc}")
            return
        with self.lock:
            self._finish_locked(
                job_id,
                "succeeded",
                "Готово",
                result=str(result) if result is not None else None,
            )

    def _finish_locked(
        self,
        job_id: str,
        status: JobStatus,
        message: str,
        log_line: str | None = None,
        result: str | None = None,
    ) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        if log_line:
            job["logs"] = [*job["logs"], log_line][-self.max_log_lines :]
        job["status"] = status
        if status == "succeeded":
            job["progress"] = 100
        job["message"] = message
        job["result"] = result
        job["finished_at"] = _now_iso()
        self.controls.pop(job_id, None)
        self._prune_locked()

    def append_log(self, job_id: str, line: str, progress: int | None) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job["logs"] = [*job["logs"], line][-self.max_log_lines :]
            if job["status"] != "cancelling":
                job["message"] = line
            if progress is not None:
                job["progress"] = progress

    def cancel(self, job_id: str) -> dict[str, str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] in TERMINAL_STATUSES:
                raise RuntimeError("Эта задача уже завершена")
            control = self.controls.get(job_id)
            if control is None:
                raise RuntimeError("Управление задачей уже недоступно")
            cancel_event, future = control
            cancel_event.set()
            if job["status"] == "queued" and future is not None and future.cancel():
                self._finish_locked(job_id, "cancelled", "Убрано из очереди")
            else:
                job["status"] = "cancelling"
                job["message"] = "Останавливаю выбранный процесс…"
            return {"id": job_id, "status": job["status"]}

    def recent(self, limit: int = 10) -> list[JobRecord]:
        with self.lock:
            ordered = sorted(
                self.jobs.values(), key=lambda item: str(item["created_at"]), reverse=True
            )[: max(1, limit)]
            return [item.copy() for item in ordered]

    def _prune_locked(self) -> None:
        if len(self.jobs) <= self.max_history:
            return
        completed = sorted(
            (
                item for item in self.jobs.values()
                if item["status"] in TERMINAL_STATUSES
            ),
            key=lambda item: str(item["created_at"]),
        )
        for item in completed[: max(0, len(self.jobs) - self.max_history)]:
            job_id = str(item["id"])
            self.jobs.pop(job_id, None)
            self.controls.pop(job_id, None)

    def shutdown(self) -> None:
        with self.lock:
            for cancel_event, _future in self.controls.values():
                cancel_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
