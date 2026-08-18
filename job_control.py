"""Адресная кооперативная отмена фоновых задач и дочерних процессов."""
from __future__ import annotations

from contextlib import contextmanager
import json
import subprocess
from threading import Event, local
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
    """Читать поток Ollama по частям, чтобы отмена срабатывала во время генерации."""
    body = dict(payload)
    body["stream"] = True
    chunks: list[str] = []
    with requests.post(url, json=body, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            checkpoint()
            if not line:
                continue
            item = json.loads(line)
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            chunks.append(str(item.get("response") or ""))
            if item.get("done"):
                break
    checkpoint()
    return "".join(chunks)
