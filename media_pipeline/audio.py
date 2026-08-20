"""Изоляция голоса, EBU R128-нормализация и сайдчейн-микширование."""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import imageio_ffmpeg  # type: ignore[import-untyped]

from job_control import checkpoint, run_process
from media_pipeline.resources import accelerator_slot, unload_ollama_model
from settings import CONFIG, ROOT_DIR

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
_LOUDNESS_JSON = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)
_STREAM_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


class SceneLike(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def duration(self) -> float: ...


@dataclass(frozen=True)
class LoudnessMeasurement:
    input_i: float
    input_lra: float
    input_tp: float
    input_thresh: float
    target_offset: float


class VoiceIsolationError(RuntimeError):
    """Оригинальный голос не удалось безопасно удалить."""


def delivery_true_peak_target() -> float:
    """Оставить запас под inter-sample overshoot финального AAC-кодирования."""
    cfg = CONFIG["audio_processing"]["loudness"]
    true_peak = float(cfg.get("true_peak_db", -1.5))
    headroom = max(0.0, float(cfg.get("codec_headroom_db", 1.0)))
    return true_peak - headroom


def validate_final_media(
    output_path: Path,
    *,
    target_duration: float,
    frame_rate: float = 30.0,
) -> LoudnessMeasurement:
    """Проверить длительность контейнера и итоговый EBU R128/true peak."""
    actual_duration = audio_duration(output_path)
    tolerance = max(0.1, 2.0 / frame_rate)
    if abs(actual_duration - target_duration) > tolerance:
        raise RuntimeError(
            "Финальный файл имеет аудио/видео-дрифт: "
            f"{actual_duration:.3f}с вместо {target_duration:.3f}с"
        )
    video_duration = stream_duration(output_path, "0:v:0", target_duration)
    audio_stream_duration = stream_duration(output_path, "0:a:0", target_duration)
    stream_tolerance = max(0.15, 4.0 / frame_rate)
    if abs(video_duration - audio_stream_duration) > stream_tolerance:
        raise RuntimeError(
            "Финальные аудио и видео имеют взаимный дрифт: "
            f"video={video_duration:.3f}с, audio={audio_stream_duration:.3f}с"
        )
    cfg = CONFIG["audio_processing"]["loudness"]
    target_i = float(cfg.get("final_lufs", -14.0))
    target_tp = float(cfg.get("true_peak_db", -1.5))
    measurement = measure_loudness(
        output_path,
        target_i=target_i,
        target_tp=target_tp,
        target_lra=11.0,
    )
    if measurement is None:
        raise RuntimeError("Финальный микс оказался цифровой тишиной")
    if abs(measurement.input_i - target_i) > 1.0:
        raise RuntimeError(
            "Итоговая громкость вне допуска: "
            f"{measurement.input_i:.1f} LUFS вместо {target_i:.1f}±1.0 LU"
        )
    if measurement.input_tp > target_tp + 0.2:
        raise RuntimeError(
            "Итоговый true peak выше допуска: "
            f"{measurement.input_tp:.1f} dBTP > {target_tp + 0.2:.1f} dBTP"
        )
    return measurement


def _run_ffmpeg(command: list[str], description: str, *, timeout: float = 900) -> None:
    checkpoint()
    result = run_process(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{description}:\n{(result.stderr or result.stdout or '')[-3500:]}"
        )


def audio_duration(path: Path) -> float:
    result = run_process(
        [FFMPEG_EXE, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr or "",
    )
    if not match:
        raise RuntimeError(f"Не удалось определить длительность аудио: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def stream_duration(path: Path, selector: str, expected_duration: float) -> float:
    """Быстро измерить последний PTS потока без декодирования и перекодирования."""
    if selector not in {"0:v:0", "0:a:0"}:
        raise ValueError(f"Неподдерживаемый selector потока: {selector}")
    result = run_process(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-i",
            str(path),
            "-map",
            selector,
            "-c",
            "copy",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        timeout=max(60.0, expected_duration * 0.25),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Не удалось проверить длительность потока {selector}:\n"
            + (result.stderr or "")[-2000:]
        )
    matches = _STREAM_TIME.findall(result.stderr or "")
    if not matches:
        raise RuntimeError(f"FFmpeg не вернул PTS для потока {selector}")
    hours, minutes, seconds = matches[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_audio_fragment(
    source_path: Path,
    start_seconds: float,
    duration: float,
    output_path: Path,
) -> Path:
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError("Длительность извлекаемого аудио должна быть положительной")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG_EXE,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(start_seconds, 0.0):.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        f"aresample=48000:async=1:first_pts=0,apad,atrim=duration={duration:.6f}",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(output_path),
    ]
    try:
        _run_ffmpeg(command, "Не удалось извлечь исходную аудиодорожку")
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    if not output_path.is_file() or output_path.stat().st_size < 1024:
        raise RuntimeError("В выбранном видео нет пригодной аудиодорожки")
    return output_path


def build_montage_bed(
    source_path: Path,
    scenes: Sequence[SceneLike],
    output_path: Path,
    target_duration: float,
) -> Path:
    if not scenes:
        raise ValueError("Для фоновой дорожки не переданы сцены")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [FFMPEG_EXE, "-y", "-hide_banner", "-loglevel", "error"]
    for scene in scenes:
        if scene.duration <= 0:
            raise ValueError("Сцена с нулевой длительностью недопустима")
        command.extend(
            [
                "-ss",
                f"{scene.start:.6f}",
                "-t",
                f"{scene.duration:.6f}",
                "-i",
                str(source_path),
            ]
        )

    labels: list[str] = []
    filters: list[str] = []
    for index, scene in enumerate(scenes):
        fade_out = max(scene.duration - 0.012, 0.0)
        filters.append(
            f"[{index}:a:0]aformat=sample_rates=48000:channel_layouts=stereo,"
            "aresample=48000:async=1:first_pts=0,asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d=0.012,afade=t=out:st={fade_out:.6f}:d=0.012[a{index}]"
        )
        labels.append(f"[a{index}]")
    filters.append(
        "".join(labels)
        + f"concat=n={len(labels)}:v=0:a=1,apad,atrim=duration={target_duration:.6f}[bed]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[bed]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(output_path),
        ]
    )
    try:
        _run_ffmpeg(command, "Не удалось собрать фоновую дорожку монтажа")
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def _demucs_python() -> Path:
    cfg = CONFIG["audio_processing"]["voice_isolation"]
    configured = str(cfg.get("python_path") or "").strip()
    candidates = [
        ROOT_DIR / configured if configured else None,
        ROOT_DIR / "audio-venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            probe = run_process(
                [str(candidate), "-c", "import demucs"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if probe.returncode == 0:
                return candidate
    raise VoiceIsolationError(
        "Demucs не установлен. Выполните: "
        "audio-venv\\Scripts\\pip.exe install -r requirements-audio.txt"
    )


def _demucs_command(
    python: Path,
    mix_path: Path,
    output_dir: Path,
    device: str,
) -> list[str]:
    cfg = CONFIG["audio_processing"]["voice_isolation"]
    return [
        str(python),
        "-m",
        "demucs",
        "--two-stems",
        "vocals",
        "--name",
        str(cfg.get("model", "htdemucs")),
        "--device",
        device,
        "--segment",
        str(int(cfg.get("segment_seconds", 7))),
        "--overlap",
        str(float(cfg.get("overlap", 0.25))),
        "--shifts",
        str(int(cfg.get("shifts", 1))),
        "--jobs",
        "0",
        "--float32",
        "--out",
        str(output_dir),
        str(mix_path),
    ]


def _is_cuda_error(message: str) -> bool:
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in ("cuda", "cudnn", "cublas", "out of memory", "nvrtc")
    )


def isolate_background(mix_path: Path, work_dir: Path) -> Path:
    cfg = CONFIG["audio_processing"]["voice_isolation"]
    if not bool(cfg.get("enabled", True)):
        raise VoiceIsolationError(
            "Voice isolation отключена, поэтому безопасная замена голоса невозможна"
        )
    checkpoint()
    from clipper.transcribe import release_models

    release_models()
    unload_ollama_model()
    python = _demucs_python()
    output_dir = work_dir / "demucs"
    model = str(cfg.get("model", "htdemucs"))
    device = str(cfg.get("device", "cuda"))
    env = os.environ.copy()
    env.setdefault("PYTORCH_NO_CUDA_MEMORY_CACHING", "1")
    env.setdefault("OMP_NUM_THREADS", str(max(1, min(4, os.cpu_count() or 1))))

    def execute(selected_device: str) -> Any:
        if selected_device == "cuda":
            with accelerator_slot("demucs"):
                return run_process(
                    _demucs_command(
                        python,
                        mix_path,
                        output_dir,
                        selected_device,
                    ),
                    capture_output=True,
                    text=True,
                    timeout=float(cfg.get("timeout_seconds", 3600)),
                    env=env,
                )
        return run_process(
            _demucs_command(
                python,
                mix_path,
                output_dir,
                selected_device,
            ),
            capture_output=True,
            text=True,
            timeout=float(cfg.get("timeout_seconds", 3600)),
            env=env,
        )

    result = execute(device)
    message = f"{result.stdout or ''}\n{result.stderr or ''}"
    if (
        result.returncode != 0
        and device == "cuda"
        and bool(cfg.get("cpu_fallback", True))
        and _is_cuda_error(message)
    ):
        print("  ! Demucs CUDA недоступен, повторяю изоляцию на CPU…")
        result = execute("cpu")
        message = f"{result.stdout or ''}\n{result.stderr or ''}"
    if result.returncode != 0:
        raise VoiceIsolationError(
            "Demucs не смог отделить оригинальный голос:\n" + message[-3500:]
        )

    background = output_dir / model / mix_path.stem / "no_vocals.wav"
    vocals = output_dir / model / mix_path.stem / "vocals.wav"
    if not background.is_file() or background.stat().st_size < 1024:
        raise VoiceIsolationError(
            f"Demucs завершился без фоновой дорожки: {background}"
        )
    if not vocals.is_file():
        raise VoiceIsolationError("Demucs не создал контрольный vocal stem")
    checkpoint()
    return background


def measure_loudness(
    input_path: Path,
    *,
    target_i: float,
    target_tp: float,
    target_lra: float,
) -> LoudnessMeasurement | None:
    filter_value = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    )
    result = run_process(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-nostats",
            "-i",
            str(input_path),
            "-af",
            filter_value,
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Не удалось измерить EBU R128 громкость:\n"
            + (result.stderr or "")[-2500:]
        )
    matches = _LOUDNESS_JSON.findall(result.stderr or "")
    if not matches:
        raise RuntimeError("FFmpeg loudnorm не вернул результаты измерения")
    payload = json.loads(matches[-1])
    values = (
        float(payload["input_i"]),
        float(payload["input_lra"]),
        float(payload["input_tp"]),
        float(payload["input_thresh"]),
        float(payload["target_offset"]),
    )
    if not all(math.isfinite(value) for value in values):
        return None
    return LoudnessMeasurement(*values)


def normalize_loudness(
    input_path: Path,
    output_path: Path,
    *,
    target_i: float,
    target_tp: float,
    target_lra: float,
    duration: float,
    limiter_db: float | None = None,
) -> Path:
    measurement = measure_loudness(
        input_path,
        target_i=target_i,
        target_tp=target_tp,
        target_lra=target_lra,
    )
    if measurement is None:
        loudness_filter = "volume=0"
    else:
        loudness_filter = (
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:"
            f"measured_I={measurement.input_i}:"
            f"measured_LRA={measurement.input_lra}:"
            f"measured_TP={measurement.input_tp}:"
            f"measured_thresh={measurement.input_thresh}:"
            f"offset={measurement.target_offset}:linear=true:print_format=summary"
        )
    filters = (
        f"{loudness_filter},aresample=48000:async=1:first_pts=0,"
        "aformat=sample_rates=48000:channel_layouts=stereo"
    )
    if limiter_db is not None:
        limiter = 10 ** (limiter_db / 20.0)
        filters += f",alimiter=limit={limiter:.6f}:level=false"
    filters += f",apad,atrim=duration={duration:.6f}"
    command = [
        FFMPEG_EXE,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-af",
        filters,
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(output_path),
    ]
    try:
        _run_ffmpeg(command, "Не удалось нормализовать аудио по EBU R128")
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def prepare_background_bed(
    mix_path: Path,
    work_dir: Path,
    *,
    duration: float,
) -> Path:
    cfg = CONFIG["audio_processing"]["loudness"]
    background = isolate_background(mix_path, work_dir)
    normalized = work_dir / "background-normalized.wav"
    return normalize_loudness(
        background,
        normalized,
        target_i=float(cfg.get("background_lufs", -24.0)),
        target_tp=float(cfg.get("true_peak_db", -1.5)),
        target_lra=float(cfg.get("lra", 11.0)),
        duration=duration,
    )


def prepare_voice(
    voice_path: Path,
    work_dir: Path,
    *,
    duration: float,
) -> Path:
    cfg = CONFIG["audio_processing"]["loudness"]
    normalized = work_dir / f"{voice_path.stem}-normalized.wav"
    return normalize_loudness(
        voice_path,
        normalized,
        target_i=float(cfg.get("voice_lufs", -16.0)),
        target_tp=float(cfg.get("true_peak_db", -1.5)),
        target_lra=float(cfg.get("voice_lra", 7.0)),
        duration=duration,
    )


def prepare_final_voice(
    voice_path: Path,
    work_dir: Path,
    *,
    duration: float,
) -> Path:
    """Нормализовать solo-озвучку сразу к громкости готового ролика."""
    cfg = CONFIG["audio_processing"]["loudness"]
    normalized = work_dir / f"{voice_path.stem}-final.wav"
    return normalize_loudness(
        voice_path,
        normalized,
        target_i=float(cfg.get("final_lufs", -14.0)),
        target_tp=float(cfg.get("true_peak_db", -1.5)),
        target_lra=float(cfg.get("voice_lra", 7.0)),
        duration=duration,
        limiter_db=delivery_true_peak_target(),
    )


def ducking_mix_filter(
    bed_input: int,
    voice_input: int,
    *,
    duration: float,
    output_label: str = "audio",
) -> str:
    cfg = CONFIG["audio_processing"]["ducking"]
    threshold = float(cfg.get("threshold", 0.035))
    ratio = float(cfg.get("ratio", 8.0))
    attack = float(cfg.get("attack_ms", 18.0))
    release = float(cfg.get("release_ms", 280.0))
    true_peak = float(
        CONFIG["audio_processing"]["loudness"].get("true_peak_db", -1.5)
    )
    limiter = 10 ** (true_peak / 20.0)
    return (
        f"[{bed_input}:a]aresample=48000:async=1:first_pts=0,"
        "aformat=sample_rates=48000:channel_layouts=stereo[bed];"
        f"[{voice_input}:a]aresample=48000:async=1:first_pts=0,"
        "aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice][key];"
        f"[bed][key]sidechaincompress=threshold={threshold:.6f}:ratio={ratio:.3f}:"
        f"attack={attack:.3f}:release={release:.3f}[ducked];"
        "[ducked][voice]amerge=inputs=2[merged];"
        "[merged]pan=stereo|c0=c0+c2|c1=c1+c3,"
        f"atrim=duration={duration:.6f},"
        f"aresample=48000:async=1:first_pts=0,"
        f"alimiter=limit={limiter:.6f}:level=false[{output_label}]"
    )


def render_ducked_mix(
    background_path: Path,
    voice_path: Path,
    work_dir: Path,
    *,
    duration: float,
) -> Path:
    """Собрать ducked mix и точно нормализовать его вторым EBU R128 проходом."""
    raw_mix = work_dir / "mix-ducked.wav"
    final_mix = work_dir / "mix-final.wav"
    command = [
        FFMPEG_EXE,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(background_path),
        "-i",
        str(voice_path),
        "-filter_complex",
        ducking_mix_filter(0, 1, duration=duration),
        "-map",
        "[audio]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(raw_mix),
    ]
    try:
        _run_ffmpeg(command, "Не удалось собрать sidechain-микс")
        cfg = CONFIG["audio_processing"]["loudness"]
        return normalize_loudness(
            raw_mix,
            final_mix,
            target_i=float(cfg.get("final_lufs", -14.0)),
            target_tp=float(cfg.get("true_peak_db", -1.5)),
            target_lra=float(cfg.get("lra", 11.0)),
            duration=duration,
            limiter_db=delivery_true_peak_target(),
        )
    finally:
        raw_mix.unlink(missing_ok=True)
