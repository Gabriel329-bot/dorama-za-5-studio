"""Bounded background jobs with durable restart recovery."""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock
from typing import Literal, TypeAlias, TypedDict, cast

from job_control import JobCancelled, cancellation_scope
from webapp.job_store import JobStore, JsonDict, StoredJob

TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "attention"}
)
RECOVERABLE_STATUSES = frozenset(
    {"queued", "running", "cancelling", "paused"}
)
JobStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "paused",
    "attention",
    "cancelled",
    "succeeded",
    "failed",
]
PersistentHandler: TypeAlias = Callable[[str, str, JsonDict], object]


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
    resume_count: int


@dataclass(frozen=True)
class JobSpec:
    payload: JsonDict
    resumable: bool


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobWriter:
    """Convert runner stdout into a bounded, durable job log."""

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
            progress = 12 + round(
                (int(match.group(1)) - 1) / int(match.group(2)) * 72
            )
        if line.startswith("Готово") or "Опубликовано" in line:
            progress = 96
        self._manager.append_log(self._job_id, line, progress)


class JobManager:
    def __init__(
        self,
        max_workers: int = 1,
        max_history: int = 100,
        max_log_lines: int = 30,
        *,
        store: JobStore | None = None,
        persistent_handler: PersistentHandler | None = None,
    ) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="dorama-studio"
        )
        self.jobs: dict[str, JobRecord] = {}
        self.controls: dict[str, tuple[Event, Future[None] | None]] = {}
        self.lock = Lock()
        self.max_history = max(10, max_history)
        self.max_log_lines = max(5, max_log_lines)
        self.store = store
        self.persistent_handler = persistent_handler
        self._specs: dict[str, JobSpec] = {}
        self._shutdown_requested = Event()
        self._started = False

    def start(self) -> int:
        """Load durable history and enqueue jobs interrupted by a restart."""
        if self._started:
            return 0
        self._started = True
        if self.store is None:
            return 0
        self.store.initialize()
        recovered: list[str] = []
        with self.lock:
            for stored in reversed(
                self.store.load_for_recovery(self.max_history)
            ):
                job = self._record_from_stored(stored)
                job_id = job["id"]
                self.jobs[job_id] = job
                self._specs[job_id] = JobSpec(
                    payload=stored.payload,
                    resumable=stored.resumable,
                )
                if job["status"] not in RECOVERABLE_STATUSES:
                    continue
                safe_to_resume = (
                    stored.resumable or job["status"] == "queued"
                )
                if safe_to_resume and self.persistent_handler is not None:
                    job["status"] = "queued"
                    job["progress"] = 4
                    job["message"] = "Продолжаю после перезапуска программы"
                    job["finished_at"] = None
                    job["resume_count"] += 1
                    job["logs"] = [
                        *job["logs"],
                        "↻ Задача восстановлена после перезапуска",
                    ][-self.max_log_lines :]
                    self._persist_locked(job_id)
                    recovered.append(job_id)
                else:
                    job["status"] = "attention"
                    job["message"] = (
                        "Автоповтор отключён: проверьте внешнюю платформу, "
                        "чтобы не создать дубль"
                    )
                    job["finished_at"] = _now_iso()
                    self._persist_locked(job_id)
            self._prune_locked()
        for job_id in recovered:
            self._enqueue_persistent(job_id)
        return len(recovered)

    @staticmethod
    def _record_from_stored(stored: StoredJob) -> JobRecord:
        raw = stored.record
        status = str(raw.get("status") or "failed")
        allowed = TERMINAL_STATUSES | RECOVERABLE_STATUSES
        if status not in allowed:
            status = "failed"
        return JobRecord(
            id=str(raw["id"]),
            kind=str(raw["kind"]),
            title=str(raw["title"]),
            status=cast(JobStatus, status),
            progress=max(0, min(int(raw.get("progress") or 0), 100)),
            message=str(raw.get("message") or ""),
            logs=[str(item) for item in raw.get("logs") or []],
            created_at=str(raw["created_at"]),
            finished_at=cast(str | None, raw.get("finished_at")),
            result=cast(str | None, raw.get("result")),
            resume_count=int(raw.get("resume_count") or 0),
        )

    def submit(
        self,
        kind: str,
        title: str,
        runner: Callable[[], object],
    ) -> JobRecord:
        """Submit an in-memory job. Kept for tests and short-lived extensions."""
        job = self._new_job(kind, title)
        job_id = job["id"]
        cancel_event = Event()
        with self.lock:
            self.jobs[job_id] = job
            self.controls[job_id] = (cancel_event, None)
            self._prune_locked()
        self._enqueue(job_id, cancel_event, runner)
        return job.copy()

    def submit_persistent(
        self,
        kind: str,
        title: str,
        payload: Mapping[str, object],
        *,
        resumable: bool = True,
    ) -> JobRecord:
        if self.store is None or self.persistent_handler is None:
            raise RuntimeError("Persistent JobManager не настроен")
        job = self._new_job(kind, title)
        job_id = job["id"]
        cancel_event = Event()
        with self.lock:
            self.jobs[job_id] = job
            self._specs[job_id] = JobSpec(dict(payload), resumable)
            self.controls[job_id] = (cancel_event, None)
            self._persist_locked(job_id)
            self._prune_locked()
        self._enqueue_persistent(job_id)
        return job.copy()

    @staticmethod
    def _new_job(kind: str, title: str) -> JobRecord:
        return JobRecord(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            title=title,
            status="queued",
            progress=4,
            message="Задача добавлена в очередь",
            logs=[],
            created_at=_now_iso(),
            finished_at=None,
            result=None,
            resume_count=0,
        )

    def _enqueue_persistent(self, job_id: str) -> None:
        if self.persistent_handler is None:
            raise RuntimeError("Не настроен обработчик восстанавливаемых задач")
        spec = self._specs[job_id]
        kind = self.jobs[job_id]["kind"]

        def runner() -> object:
            assert self.persistent_handler is not None
            return self.persistent_handler(job_id, kind, spec.payload)

        cancel_event = Event()
        with self.lock:
            self.controls[job_id] = (cancel_event, None)
        self._enqueue(job_id, cancel_event, runner)

    def _enqueue(
        self,
        job_id: str,
        cancel_event: Event,
        runner: Callable[[], object],
    ) -> None:
        future = self.executor.submit(
            self._run, job_id, cancel_event, runner
        )
        with self.lock:
            if job_id in self.controls:
                self.controls[job_id] = (cancel_event, future)

    def _run(
        self,
        job_id: str,
        cancel_event: Event,
        runner: Callable[[], object],
    ) -> None:
        writer = JobWriter(self, job_id)
        with self.lock:
            if cancel_event.is_set():
                self._finish_cancelled_or_paused_locked(job_id)
                return
            job = self.jobs[job_id]
            job["status"] = "running"
            job["progress"] = max(job["progress"], 8)
            job["message"] = "Запускаю обработку"
            self._persist_locked(job_id)
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
                self._finish_cancelled_or_paused_locked(job_id)
            return
        except Exception as exc:  # noqa: BLE001
            writer.flush()
            with self.lock:
                self._finish_locked(
                    job_id,
                    "failed",
                    str(exc),
                    f"Ошибка: {exc}",
                )
            return
        with self.lock:
            self._finish_locked(
                job_id,
                "succeeded",
                "Готово",
                result=str(result) if result is not None else None,
            )

    def _finish_cancelled_or_paused_locked(self, job_id: str) -> None:
        if self._shutdown_requested.is_set() and job_id in self._specs:
            spec = self._specs[job_id]
            if spec.resumable:
                self._pause_locked(
                    job_id,
                    "Приостановлено при завершении программы",
                )
            else:
                self._finish_locked(
                    job_id,
                    "attention",
                    "Прервано: перед повтором проверьте внешнюю платформу",
                    "Автоповтор отключён для защиты от дублей",
                )
            return
        self._finish_locked(
            job_id,
            "cancelled",
            "Отменено пользователем",
            "Процесс остановлен",
        )

    def _pause_locked(self, job_id: str, message: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job["status"] = "paused"
        job["message"] = message
        job["finished_at"] = None
        job["logs"] = [*job["logs"], "⏸ " + message][-self.max_log_lines :]
        self.controls.pop(job_id, None)
        self._persist_locked(job_id)

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
        self._persist_locked(job_id)
        self._prune_locked()

    def append_log(
        self,
        job_id: str,
        line: str,
        progress: int | None,
    ) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job["logs"] = [*job["logs"], line][-self.max_log_lines :]
            if job["status"] != "cancelling":
                job["message"] = line
            if progress is not None:
                job["progress"] = progress
            self._persist_locked(job_id)

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
            if (
                job["status"] == "queued"
                and future is not None
                and future.cancel()
            ):
                self._finish_locked(
                    job_id, "cancelled", "Убрано из очереди"
                )
            else:
                job["status"] = "cancelling"
                job["message"] = "Останавливаю выбранный процесс…"
                self._persist_locked(job_id)
            return {"id": job_id, "status": job["status"]}

    def recent(self, limit: int = 10) -> list[JobRecord]:
        with self.lock:
            ordered = sorted(
                self.jobs.values(),
                key=lambda item: str(item["created_at"]),
                reverse=True,
            )[: max(1, limit)]
            return [item.copy() for item in ordered]

    def _persist_locked(self, job_id: str) -> None:
        if self.store is None or job_id not in self._specs:
            return
        spec = self._specs[job_id]
        self.store.save(
            self.jobs[job_id],
            spec.payload,
            resumable=spec.resumable,
        )

    def _prune_locked(self) -> None:
        if len(self.jobs) <= self.max_history:
            return
        completed = sorted(
            (
                item
                for item in self.jobs.values()
                if item["status"] in TERMINAL_STATUSES
            ),
            key=lambda item: str(item["created_at"]),
        )
        deleted: list[str] = []
        for item in completed[
            : max(0, len(self.jobs) - self.max_history)
        ]:
            job_id = str(item["id"])
            self.jobs.pop(job_id, None)
            self.controls.pop(job_id, None)
            self._specs.pop(job_id, None)
            deleted.append(job_id)
        if self.store is not None:
            self.store.delete(deleted)

    def shutdown(self) -> None:
        """Cooperatively stop work and durably mark it for the next start."""
        self._shutdown_requested.set()
        futures: list[Future[None]] = []
        with self.lock:
            for job_id, (cancel_event, future) in list(
                self.controls.items()
            ):
                cancel_event.set()
                if future is not None:
                    futures.append(future)
                if future is not None and future.cancel():
                    job = self.jobs.get(job_id)
                    if (
                        job is not None
                        and job_id in self._specs
                        and job["status"] == "queued"
                    ):
                        job["message"] = (
                            "Ожидает продолжения после запуска программы"
                        )
                        self.controls.pop(job_id, None)
                        self._persist_locked(job_id)
                    else:
                        self._finish_cancelled_or_paused_locked(job_id)
                    continue
                if job_id in self._specs:
                    spec = self._specs[job_id]
                    if spec.resumable:
                        self._pause_locked(
                            job_id,
                            "Приостановлено при завершении программы",
                        )
                    else:
                        self._finish_locked(
                            job_id,
                            "attention",
                            "Прервано: перед повтором проверьте внешнюю платформу",
                        )
        if futures:
            wait(futures, timeout=10.0)
        self.executor.shutdown(wait=False, cancel_futures=True)
