"""Рендер оригинальной карточки 9:16 с озвучкой и субтитрами."""
import os
import re
import textwrap
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont

from clipper.render import _ass_escape_path
from clipper.transcribe import release_models, transcribe
from job_control import run_process
from media_pipeline.audio import (
    audio_duration,
    prepare_final_voice,
    validate_final_media,
)
from media_pipeline.resources import render_slot
from media_pipeline.subtitles import build_cues, write_ass
from video_accel import selected_encoder_options


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    names = [
        windows_fonts / ("arialbd.ttf" if bold else "arial.ttf"),
        windows_fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(str(name), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def create_cover(title: str, output_path: Path) -> Path:
    width, height = 1080, 1920
    image = Image.new("RGB", (width, height), "#0C0713")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        color = (int(12 + 35 * ratio), int(7 + 8 * ratio), int(19 + 42 * ratio))
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((650, -120, 1210, 440), fill="#7E2448")
    draw.ellipse((-260, 1300, 460, 2020), fill="#251F70")
    draw.rounded_rectangle((70, 115, 520, 195), radius=38, fill="#E8B4C8")
    draw.text((105, 132), "ДОРАМА • РАДАР", font=_font(34, True), fill="#2C1020")

    title_font = _font(82, True)
    lines = textwrap.wrap(title, width=20)[:5]
    line_height = 110
    block_height = len(lines) * line_height
    start_y = max(430, (height - block_height) // 2 - 80)
    for index, line in enumerate(lines):
        draw.text((80, start_y + index * line_height), line, font=title_font, fill="#FFFFFF")
    draw.text((82, start_y + block_height + 70), "Коротко о том, что обсуждают сейчас", font=_font(38), fill="#E8B4C8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=94)
    return output_path


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]


def create_subtitles(
    voice_path: Path,
    duration: float,
    output_path: Path,
    *,
    scene_cuts: list[float] | None = None,
) -> Path:
    """Строить субтитры только по фактическим word timestamps готовой озвучки."""
    try:
        transcript = transcribe(str(voice_path), language="ru")
        cues = build_cues(
            transcript.words,
            duration,
            scene_cuts=scene_cuts or (),
        )
        return write_ass(cues, output_path)
    finally:
        release_models()


def _audio_duration(audio_path: Path) -> float:
    return audio_duration(audio_path)


def _fit_audio(audio_path: Path, target_duration: float, output_path: Path) -> Path:
    source_duration = _audio_duration(audio_path)
    factor = source_duration / target_duration
    if factor < 0.80 or factor > 1.25:
        raise RuntimeError(
            f"Озвучка длится {source_duration:.0f}с — приведение к {target_duration:.0f}с "
            f"(коэффициент {factor:.2f}) ухудшит качество голоса. "
            f"Измените целевое число слов, чтобы коэффициент был от 0.80 до 1.25."
        )
    result = run_process(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-i",
            str(audio_path),
            "-filter:a",
            f"atempo={factor:.6f},aresample=48000:async=1:first_pts=0",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le" if output_path.suffix.lower() == ".wav" else "aac",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Не удалось привести озвучку к пяти минутам:\n{result.stderr[-2000:]}")
    return output_path


def render_video(
    cover_path: Path,
    audio_path: Path,
    narration: str,
    output_path: Path,
    target_duration: float | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.with_suffix(".ass")
    fitted_audio = output_path.with_suffix(".fitted.wav")
    if target_duration:
        _fit_audio(audio_path, target_duration, fitted_audio)
        render_audio = fitted_audio
        duration = target_duration
    else:
        render_audio = audio_path
        duration = _audio_duration(audio_path)
    normalized_audio = prepare_final_voice(
        render_audio,
        render_audio.parent,
        duration=duration,
    )
    create_subtitles(normalized_audio, duration, ass_path)
    encoder, encoder_args = selected_encoder_options(still_image=True)
    print(f"  FFmpeg encoder: {encoder}")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loop", "1", "-framerate", "30", "-i", str(cover_path),
        "-i", str(normalized_audio), "-vf", f"subtitles='{_ass_escape_path(ass_path)}'",
        *encoder_args,
        "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p",
        "-r", "30", "-t", f"{duration:.6f}", "-shortest", str(output_path),
    ]
    try:
        with render_slot(encoder):
            result = run_process(command, capture_output=True, text=True)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        ass_path.unlink(missing_ok=True)
        fitted_audio.unlink(missing_ok=True)
        normalized_audio.unlink(missing_ok=True)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg не смог собрать дорама-ролик:\n{result.stderr[-2500:]}")
    try:
        validate_final_media(output_path, target_duration=duration)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
