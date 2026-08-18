"""Выбор доступного H.264-кодировщика с проверкой реального запуска."""
from __future__ import annotations

from functools import lru_cache

import imageio_ffmpeg

from job_control import run_process
from settings import CONFIG

HARDWARE_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")


def encoder_options(encoder: str, *, still_image: bool = False) -> list[str]:
    cfg = CONFIG["render"]
    quality = str(cfg.get("hardware_quality", 22))
    if encoder == "h264_nvenc":
        return [
            "-c:v", encoder,
            "-preset", str(cfg.get("hardware_preset", "p4")),
            "-tune", "hq",
            "-rc", "vbr",
            "-cq:v", quality,
            "-b:v", "0",
        ]
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-preset", "medium", "-global_quality", quality]
    if encoder == "h264_amf":
        return ["-c:v", encoder, "-quality", "speed", "-rc", "cqp", "-qp_i", quality, "-qp_p", quality]
    options = [
        "-c:v", "libx264",
        "-preset", str(cfg.get("software_preset", "veryfast")),
        "-crf", str(cfg.get("software_crf", 21)),
    ]
    if still_image:
        options.extend(["-tune", "stillimage"])
    return options


@lru_cache(maxsize=1)
def _listed_encoders() -> set[str]:
    result = run_process(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        encoder
        for encoder in (*HARDWARE_ENCODERS, "libx264")
        if encoder in (result.stdout or "")
    }


@lru_cache(maxsize=None)
def _probe_encoder(encoder: str) -> bool:
    if encoder not in _listed_encoders():
        return False
    if encoder == "libx264":
        return True
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", "color=c=black:s=64x64:r=1:d=0.1",
        "-frames:v", "1",
        *encoder_options(encoder),
        "-f", "null",
        "-",
    ]
    result = run_process(command, capture_output=True, text=True, timeout=20)
    return result.returncode == 0


@lru_cache(maxsize=1)
def select_video_encoder() -> str:
    requested = str(CONFIG["render"].get("video_encoder", "auto")).lower()
    candidates = HARDWARE_ENCODERS if requested == "auto" else (requested,)
    for encoder in candidates:
        if _probe_encoder(encoder):
            return encoder
    return "libx264"


def selected_encoder_options(*, still_image: bool = False) -> tuple[str, list[str]]:
    encoder = select_video_encoder()
    return encoder, encoder_options(encoder, still_image=still_image)
