"""Координация GPU-этапов и освобождение памяти локальных моделей."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

import requests

from job_control import checkpoint
from settings import CONFIG

log = logging.getLogger("dorama-resources")
_accelerator_lock = Lock()


@contextmanager
def accelerator_slot(stage: str) -> Iterator[None]:
    """Сериализовать тяжёлые GPU-этапы с отменяемым ожиданием."""
    while not _accelerator_lock.acquire(timeout=0.25):
        checkpoint()
    try:
        log.info("GPU-этап начат: %s", stage)
        yield
    finally:
        _accelerator_lock.release()
        log.info("GPU-этап завершён: %s", stage)


@contextmanager
def render_slot(encoder: str) -> Iterator[None]:
    """NVENC делит видеопамять с ML; CPU/QSV/AMF блокировать не нужно."""
    if encoder == "h264_nvenc":
        with accelerator_slot("ffmpeg-nvenc"):
            yield
        return
    yield


def unload_ollama_model() -> bool:
    """Попросить Ollama немедленно освободить модель и VRAM."""
    cfg = CONFIG.get("highlight", {})
    host = str(cfg.get("ollama_host", "http://localhost:11434")).rstrip("/")
    model = str(cfg.get("model", "")).strip()
    if not model:
        return False
    try:
        response = requests.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
            timeout=(1.0, 15.0),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Ollama не подтвердила освобождение модели: %s", exc)
        return False
    log.info("Ollama освободила модель %s", model)
    return True
