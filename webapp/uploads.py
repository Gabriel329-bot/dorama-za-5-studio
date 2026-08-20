"""Ограниченная потоковая загрузка и быстрая проверка контейнера видео."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import BinaryIO

ALLOWED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})
COPY_CHUNK_BYTES = 1024 * 1024


class UploadValidationError(ValueError):
    pass


class UploadTooLargeError(UploadValidationError):
    pass


def _looks_like_video(path: Path) -> bool:
    with path.open("rb") as source:
        header = source.read(16)
    return (
        (len(header) >= 12 and header[4:8] == b"ftyp")
        or header.startswith(b"\x1aE\xdf\xa3")
        or (header.startswith(b"RIFF") and header[8:12] == b"AVI ")
    )


def save_video_upload(
    stream: BinaryIO,
    original_filename: str,
    destination_dir: Path,
    max_bytes: int,
) -> tuple[Path, int]:
    if max_bytes <= 0:
        raise ValueError("max_bytes должен быть положительным")
    suffix = Path(original_filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise UploadValidationError("Поддерживаются MP4, MOV, MKV, WEBM и AVI")
    safe_stem = re.sub(
        r"[^\wа-яА-ЯёЁ-]+", "_", Path(original_filename or "video").stem
    ).strip("_")[:80]
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / (
        f"{uuid.uuid4().hex}_{safe_stem or 'video'}{suffix}"
    )
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := stream.read(COPY_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"Файл превышает лимит {max_bytes // 1024 // 1024} МБ"
                    )
                output.write(chunk)
        if total == 0:
            raise UploadValidationError("Загружен пустой файл")
        if not _looks_like_video(destination):
            raise UploadValidationError("Содержимое файла не похоже на поддерживаемое видео")
        return destination, total
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
