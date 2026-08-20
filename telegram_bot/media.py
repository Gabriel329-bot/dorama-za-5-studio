"""Подготовка безопасного по размеру предпросмотра для Telegram."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

from webapp.uploads import probe_video_duration

TELEGRAM_PREVIEW_LIMIT_MB = 45


def prepare_telegram_preview(
    source: Path,
    work_dir: Path,
    *,
    limit_mb: int = TELEGRAM_PREVIEW_LIMIT_MB,
) -> Path:
    """Вернуть исходник либо перекодированный MP4, помещающийся в лимит бота."""
    if not source.is_file():
        raise FileNotFoundError(source)
    max_bytes = max(5, limit_mb) * 1024 * 1024
    if source.suffix.lower() == ".mp4" and source.stat().st_size <= max_bytes:
        return source

    duration = probe_video_duration(source)
    if duration <= 0:
        raise RuntimeError("Не удалось определить длительность предпросмотра")
    work_dir.mkdir(parents=True, exist_ok=True)
    output = work_dir / "telegram-preview.mp4"

    audio_bitrate_kbps = 64
    budget_bits = max_bytes * 8 * 0.90
    total_kbps = int(budget_bits / duration / 1000)
    video_kbps = max(180, min(1100, total_kbps - audio_bitrate_kbps))
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        (
            "scale=360:640:force_original_aspect_ratio=decrease,"
            "pad=360:640:(ow-iw)/2:(oh-ih)/2:color=black"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{int(video_kbps * 1.25)}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate_kbps}k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        creationflags=creation_flags,
    )
    if completed.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        detail = completed.stderr.strip()[-1000:] or "FFmpeg не создал файл"
        raise RuntimeError(f"Не удалось подготовить Telegram-превью: {detail}")
    if output.stat().st_size > max_bytes:
        output.unlink(missing_ok=True)
        raise RuntimeError("Telegram-превью превышает безопасный размер после сжатия")
    return output
