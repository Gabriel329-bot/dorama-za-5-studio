"""Адресная кооперативная отмена фоновых задач и дочерних процессов."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
from typing import Any, ParamSpec, TypeVar

import requests


class JobCancelled(RuntimeError):
    """Текущая задача отменена пользователем."""


_active_event: ContextVar[Event | None] = ContextVar("active_cancellation_event", default=None)
P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def cancellation_scope(event: Event) -> Iterator[None]:
    token = _active_event.set(event)
    try:
        checkpoint()
        yield
    finally:
        _active_event.reset(token)


def checkpoint() -> None:
    event = _active_event.get()
    if event is not None and event.is_set():
        raise JobCancelled("Процесс отменён пользователем")


def submit_cancellable(
    executor: Executor,
    function: Callable[P, R],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Future[R]:
    """Передать текущий cancellation context в новый поток executor."""
    context = copy_context()

    def invoke() -> R:
        return context.run(function, *args, **kwargs)

    return executor.submit(invoke)


def cancellable_wait(seconds: float, interval: float = 0.25) -> None:
    """Ожидание с быстрой реакцией на адресную отмену."""
    deadline = monotonic() + max(seconds, 0.0)
    event = _active_event.get()
    passive_waiter = Event() if event is None else event
    while True:
        checkpoint()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        passive_waiter.wait(min(interval, remaining))


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Завершить и дочерние FFmpeg-процессы yt-dlp, не оставляя сирот."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Аналог subprocess.run, который быстро завершает дочерний процесс при отмене."""
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("capture_output нельзя сочетать с stdout/stderr")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = subprocess.Popen(command, text=text, **kwargs)
    started = monotonic()
    try:
        while True:
            checkpoint()
            remaining = None if timeout is None else timeout - (monotonic() - started)
            if remaining is not None and remaining <= 0:
                assert timeout is not None
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.35, remaining) if remaining is not None else 0.35)
                break
            except subprocess.TimeoutExpired:
                continue
    except (JobCancelled, subprocess.TimeoutExpired):
        _terminate_process_tree(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return result


def ollama_generate(url: str, payload: Mapping[str, Any], timeout: float = 600) -> str:
    """Читать Ollama в отдельном потоке, сохраняя отмену до появления первого токена."""
    body = dict(payload)
    body["stream"] = True
    body.setdefault("keep_alive", "15m")
    chunks: list[str] = []
    messages: Queue[tuple[str, object]] = Queue()
    response_holder: dict[str, requests.Response] = {}
    session = requests.Session()

    def read_stream() -> None:
        response: requests.Response | None = None
        try:
            response = session.post(url, json=body, stream=True, timeout=(10, max(timeout, 60)))
            response_holder["response"] = response
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    messages.put(("line", line))
            messages.put(("done", None))
        except BaseException as exc:  # noqa: BLE001 — передаём ошибку главному потоку
            messages.put(("error", exc))
        finally:
            if response is not None:
                response.close()

    worker = Thread(target=read_stream, name="ollama-stream", daemon=True)
    worker.start()
    started = monotonic()
    next_notice = 20.0
    try:
        while True:
            checkpoint()
            elapsed = monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                raise requests.Timeout(f"Ollama не завершила ответ за {timeout:.0f} секунд")
            try:
                kind, value = messages.get(timeout=min(0.25, remaining))
            except Empty:
                if elapsed >= next_notice:
                    print(f"  Ollama обрабатывает контекст… {elapsed:.0f}с")
                    next_notice += 30.0
                continue
            if kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError(str(value))
            if kind == "done":
                break
            if not isinstance(value, (str, bytes, bytearray)):
                raise TypeError("Ollama вернула неожиданный тип потокового сообщения")
            item = json.loads(value)
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            chunks.append(str(item.get("response") or ""))
            if item.get("done"):
                break
        checkpoint()
        return "".join(chunks)
    finally:
        response = response_holder.get("response")
        if response is not None and worker.is_alive():
            response.close()
        session.close()
