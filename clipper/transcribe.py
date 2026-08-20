"""Транскрибация видео с CUDA fallback и дисковым кэшем результата."""
from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

_cuda_dll_handles: list[object] = []


def _configure_cuda_dlls() -> None:
    """Подключить официальные NVIDIA wheels из текущего venv, не меняя систему."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    site_packages = Path(sys.executable).resolve().parent.parent / "Lib" / "site-packages"
    project_root = Path(os.environ.get("DORAMA_HOME", "")).resolve()
    candidates = (
        project_root / "venv" / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin",
        project_root / "venv" / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
    )
    for directory in candidates:
        if not directory.is_dir():
            continue
        value = str(directory)
        if value.casefold() not in os.environ.get("PATH", "").casefold():
            os.environ["PATH"] = f"{value}{os.pathsep}{os.environ.get('PATH', '')}"
        _cuda_dll_handles.append(os.add_dll_directory(value))


_configure_cuda_dlls()

from faster_whisper import (  # type: ignore[import-untyped]
    WhisperModel,
)

from job_control import checkpoint
from media_pipeline.resources import accelerator_slot
from settings import CACHE_DIR, CONFIG
from storage.files import atomic_write_text

_model_cache: dict[tuple[str, str, str], WhisperModel] = {}
_model_lock = RLock()
CACHE_VERSION = 2


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    full_text: str
    words: list[Word]


def _compute_type(device: str) -> str:
    configured = str(CONFIG["whisper"].get("compute_type", "auto"))
    if configured != "auto":
        return configured
    return "float16" if device == "cuda" else "int8"


def _get_model(device: str | None = None) -> WhisperModel:
    checkpoint()
    model_size = str(CONFIG["whisper"]["model"])
    selected_device = device or str(CONFIG["whisper"].get("device", "cpu"))
    compute_type = _compute_type(selected_device)
    key = (model_size, selected_device, compute_type)
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(model_size, device=selected_device, compute_type=compute_type)
    return _model_cache[key]


def release_models() -> None:
    """Освободить модели Whisper перед другим GPU-этапом."""
    with _model_lock, accelerator_slot("whisper"):
        _model_cache.clear()
        gc.collect()


def _transcript_cache_key(video_path: str | Path, language: str | None) -> str:
    path = Path(video_path).resolve()
    stat = path.stat()
    payload = {
        "version": CACHE_VERSION,
        "path": str(path).casefold(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "model": CONFIG["whisper"]["model"],
        "language": language,
        "word_timestamps": True,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(video_path: str | Path, language: str | None) -> Path:
    return Path(CACHE_DIR) / "transcripts" / f"{_transcript_cache_key(video_path, language)}.json"


def _load_cached_transcript(video_path: str | Path, language: str | None) -> Transcript | None:
    path = _cache_path(video_path, language)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != CACHE_VERSION:
            return None
        words: list[Word] = []
        previous_start = 0.0
        for item in payload["words"]:
            word = Word(
                str(item["text"]),
                float(item["start"]),
                float(item["end"]),
            )
            if (
                not word.text
                or not all(
                    math.isfinite(value)
                    for value in (word.start, word.end)
                )
                or word.start < 0
                or word.end <= word.start
                or word.start + 0.25 < previous_start
            ):
                raise ValueError("Некорректные таймкоды в кэше Whisper")
            previous_start = word.start
            words.append(word)
        if not words:
            return None
        return Transcript(str(payload["full_text"]), words)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def _save_cached_transcript(video_path: str | Path, language: str | None, transcript: Transcript) -> None:
    path = _cache_path(video_path, language)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "full_text": transcript.full_text,
        "words": [
            {"text": word.text, "start": word.start, "end": word.end}
            for word in transcript.words
        ],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


def _is_cuda_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "cuda",
        "cudnn",
        "cublas",
        "nvidia",
        "out of memory",
        "device is busy",
    ))


def _run_transcription(model: WhisperModel, video_path: str, language: str | None) -> Transcript:
    segments, _info = model.transcribe(
        video_path,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=1,
    )

    words: list[Word] = []
    text_parts: list[str] = []
    for segment in segments:
        checkpoint()
        text_parts.append(segment.text.strip())
        if segment.words:
            for word in segment.words:
                words.append(Word(text=word.word.strip(), start=word.start, end=word.end))
    if not words:
        raise RuntimeError("Whisper не распознал речь в видео — проверь, что в файле есть звук и голос.")
    return Transcript(full_text=" ".join(text_parts), words=words)


def transcribe(video_path: str, language: str | None = None) -> Transcript:
    """Вернуть полный текст и слова с таймкодами; повторный вызов читает дисковый кэш."""
    checkpoint()
    selected_language = CONFIG["whisper"]["language"] if language is None else language
    if selected_language == "auto":
        selected_language = None

    cached = _load_cached_transcript(video_path, selected_language)
    if cached is not None:
        print(f"  Whisper cache: использую сохранённую расшифровку ({len(cached.words)} слов)")
        return cached

    configured_device = str(CONFIG["whisper"].get("device", "cpu"))
    try:
        with _model_lock:
            try:
                if configured_device == "cuda":
                    with accelerator_slot("whisper"):
                        result = _run_transcription(
                            _get_model(configured_device),
                            video_path,
                            selected_language,
                        )
                else:
                    result = _run_transcription(
                        _get_model(configured_device),
                        video_path,
                        selected_language,
                    )
            except Exception as exc:
                can_fallback = (
                    configured_device == "cuda"
                    and CONFIG["whisper"].get("cuda_fallback", True)
                    and _is_cuda_failure(exc)
                )
                if not can_fallback:
                    raise
                print(
                    "  ! CUDA для Whisper недоступна, продолжаю на CPU: "
                    f"{type(exc).__name__}"
                )
                result = _run_transcription(
                    _get_model("cpu"),
                    video_path,
                    selected_language,
                )
        checkpoint()
        _save_cached_transcript(video_path, selected_language, result)
        return result
    except BaseException:
        release_models()
        raise
