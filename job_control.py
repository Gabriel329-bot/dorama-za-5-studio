"""Адресная кооперативная отмена фоновых задач и дочерних процессов."""
from __future__ import annotations

from contextlib import contextmanager
import json
from queue import Empty, Queue
import subprocess
from threading import Event, Thread, local
from time import monotonic
from typing import Iterator

import requests


class JobCancelled(RuntimeError):
    """Текущая задача отменена пользователем."""


_state = local()


@contextmanager
def cancellation_scope(event: Event) -> Iterator[None]:
    previous = getattr(_state, "event", None)
    _state.event = event
    try:
        checkpoint()
        yield
    finally:
        _state.event = previous


def checkpoint() -> None:
    event = getattr(_state, "event", None)
    if event is not None and event.is_set():
        raise JobCancelled("Процесс отменён пользователем")


def run_process(command, *, capture_output: bool = False, text: bool = False,
                timeout: float | None = None, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
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
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.35, remaining) if remaining is not None else 0.35)
                break
            except subprocess.TimeoutExpired:
                continue
    except (JobCancelled, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return result


def ollama_generate(url: str, payload: dict, timeout: float = 600) -> str:
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
        except BaseException as exc:  # передаём исходный тип сетевой ошибки главному потоку
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
