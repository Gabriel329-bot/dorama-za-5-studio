"""Прямой перевод выбранного видеофрагмента без сюжетного пересказа."""
from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

from clipper.render import _ass_escape_path
from clipper.transcribe import Transcript, release_models, transcribe
from dorama.render import _audio_duration, create_subtitles
from dorama.script import _normalize_hashtags
from dorama.source_pipeline import (
    TimelineItem,
    _make_timeline,
    _russian_word_count,
    _translate_timeline,
    _video_duration,
)
from dorama.speech import synthesize
from job_control import checkpoint, run_process
from media_pipeline.audio import (
    extract_audio_fragment,
    prepare_background_bed,
    prepare_voice,
    render_ducked_mix,
    validate_final_media,
)
from media_pipeline.resources import render_slot, unload_ollama_model
from media_pipeline.subtitles import detect_scene_cuts
from settings import CONFIG, PENDING_DIR
from storage import db
from storage.files import atomic_write_text
from video_accel import selected_encoder_options

MIN_TRANSLATION_SECONDS = 10.0
MAX_TRANSLATION_SECONDS = 300.0


def validate_translation_interval(
    start_seconds: float,
    end_seconds: float,
    source_duration: float,
) -> tuple[float, float]:
    values = (start_seconds, end_seconds, source_duration)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Таймкоды должны быть конечными числами")
    start = max(0.0, float(start_seconds))
    end = float(end_seconds)
    if source_duration <= 0:
        raise ValueError("Исходное видео имеет некорректную длительность")
    if end > source_duration + 0.25:
        raise ValueError("Конец фрагмента выходит за длительность видео")
    end = min(end, source_duration)
    selected_duration = end - start
    if selected_duration < MIN_TRANSLATION_SECONDS:
        raise ValueError("Выберите фрагмент длительностью не меньше 10 секунд")
    if selected_duration > MAX_TRANSLATION_SECONDS + 0.01:
        raise ValueError("Для перевода выберите фрагмент не длиннее 5 минут")
    return start, end


def _artifact_id() -> str:
    now = datetime.now(timezone.utc).astimezone()
    return f"{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def _slug(text: str) -> str:
    value = re.sub(r"[^\wа-яА-ЯёЁ-]+", "_", text, flags=re.UNICODE).strip("_")
    return value[:52] or "literal_translation"


def _format_time(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _translation_timeline(transcript: Transcript) -> list[TimelineItem]:
    # Короткие бакеты сохраняют порядок прямой речи и не превращают перевод
    # в общий пересказ. Для фрагмента до пяти минут общий лимит не нужен.
    return _make_timeline(
        transcript.words,
        bucket_seconds=20,
        max_chars=100_000,
    )


def _fit_translation_audio(
    audio_path: Path,
    target_duration: float,
    output_path: Path,
) -> Path:
    voice_duration = _audio_duration(audio_path)
    if voice_duration <= 0:
        raise RuntimeError("Озвучка перевода имеет некорректную длительность")
    if voice_duration > target_duration:
        factor = voice_duration / target_duration
        if factor > 1.35:
            raise RuntimeError(
                "Русская озвучка не помещается в выбранный фрагмент без сильного "
                "ускорения. Выберите более длинный интервал."
            )
        audio_filter = f"atempo={factor:.6f}"
    else:
        padding = max(target_duration - voice_duration, 0.0)
        audio_filter = f"apad=pad_dur={padding:.6f}"
    audio_filter += ",aresample=48000:async=1:first_pts=0"
    result = run_process(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-i",
            str(audio_path),
            "-filter:a",
            audio_filter,
            "-t",
            f"{target_duration:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Не удалось подготовить озвучку перевода:\n{result.stderr[-2000:]}")
    return output_path


def _render_translation(
    source_path: Path,
    start_seconds: float,
    target_duration: float,
    voice_path: Path,
    original_mix: Path,
    output_path: Path,
    work_dir: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path = output_path.with_suffix(".ass")
    fitted_voice = work_dir / "voice-fitted.wav"
    background = prepare_background_bed(
        original_mix,
        work_dir,
        duration=target_duration,
    )
    _fit_translation_audio(voice_path, target_duration, fitted_voice)
    voice = prepare_voice(
        fitted_voice,
        work_dir,
        duration=target_duration,
    )
    final_audio = render_ducked_mix(
        background,
        voice,
        work_dir,
        duration=target_duration,
    )
    scene_cuts = detect_scene_cuts(
        source_path,
        start_seconds=start_seconds,
        duration=target_duration,
    )
    create_subtitles(
        voice,
        target_duration,
        subtitle_path,
        scene_cuts=scene_cuts,
    )
    encoder, encoder_args = selected_encoder_options()
    print(f"  FFmpeg encoder: {encoder}")
    escaped_subtitles = _ass_escape_path(subtitle_path)
    filters = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgx];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgx];"
        "[bgx][fgx]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,"
        f"format=yuv420p,subtitles='{escaped_subtitles}'[video]"
    )
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{target_duration:.3f}",
        "-i",
        str(source_path),
        "-i",
        str(final_audio),
        "-filter_complex",
        filters,
        "-map",
        "[video]",
        "-map",
        "1:a:0",
        *encoder_args,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-t",
        f"{target_duration:.3f}",
        str(output_path),
    ]
    try:
        with render_slot(encoder):
            result = run_process(command, capture_output=True, text=True)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        subtitle_path.unlink(missing_ok=True)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg не смог собрать прямой перевод:\n{result.stderr[-3500:]}"
        )
    try:
        validate_final_media(
            output_path,
            target_duration=target_duration,
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def create_literal_translation(
    source_path: Path,
    start_seconds: float,
    end_seconds: float,
    operation_id: str | None = None,
) -> Path:
    source = source_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_duration = _video_duration(source)
    start, end = validate_translation_interval(
        start_seconds,
        end_seconds,
        source_duration,
    )
    target_duration = end - start
    artifact_id = _artifact_id()
    work_root = PENDING_DIR / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    output_path: Path = PENDING_DIR / (
        f"{artifact_id}_{_slug('Перевод_' + source.stem)}.mp4"
    )
    audit_path = output_path.with_suffix(".translation.json")

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"translation_{artifact_id}_",
            dir=work_root,
        ) as temporary:
            work_dir = Path(temporary)
            audio_fragment = work_dir / "fragment.wav"
            voice_path = work_dir / "voice.mp3"

            print(
                "[1/5] Извлекаю выбранный фрагмент "
                f"{_format_time(start)}–{_format_time(end)}..."
            )
            checkpoint()
            extract_audio_fragment(
                source,
                start,
                target_duration,
                audio_fragment,
            )

            print("[2/5] Распознаю всю речь выбранного фрагмента без пересказа...")
            checkpoint()
            transcript = transcribe(
                str(audio_fragment),
                language=str(CONFIG["episode"].get("source_language", "auto")),
            )
            release_models()
            timeline = _translation_timeline(transcript)
            if not timeline:
                raise RuntimeError("Whisper не распознал речь выбранного фрагмента")

            print("[3/5] Перевожу реплики на русский без сокращения сюжета...")
            checkpoint()
            translated = _translate_timeline(timeline)
            narration = re.sub(
                r"\s+",
                " ",
                " ".join(item.text for item in translated),
            ).strip()
            if _russian_word_count(narration) < 8:
                raise RuntimeError("Не удалось получить русский перевод выбранного фрагмента")

            print("[4/5] Создаю русскую озвучку и синхронизированные субтитры...")
            checkpoint()
            release_models()
            unload_ollama_model()
            asyncio.run(
                synthesize(
                    narration,
                    voice_path,
                    str(CONFIG["dorama"]["voice"]),
                    str(CONFIG["dorama"]["rate"]),
                )
            )

            print("[5/5] Монтирую выбранный интервал без перестановки сцен...")
            checkpoint()
            _render_translation(
                source,
                start,
                target_duration,
                voice_path,
                audio_fragment,
                output_path,
                work_dir,
            )

        title = f"Перевод фрагмента: {source.stem}"[:100]
        caption = (
            "Прямой русский перевод выбранного фрагмента "
            f"{_format_time(start)}–{_format_time(end)} без сюжетного пересказа."
        )
        hashtags = _normalize_hashtags(
            ["#переводдорамы", "#русскаяозвучка"],
            CONFIG["dorama"]["base_hashtags"],
        )
        full_caption = f"{caption}\n\n{' '.join(hashtags)}"
        atomic_write_text(
            audit_path,
            json.dumps(
                {
                    "mode": "literal_translation",
                    "source": str(source),
                    "source_duration": source_duration,
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration": target_duration,
                    "transcript_words": len(transcript.words),
                    "translated_words": _russian_word_count(narration),
                    "title": title,
                    "caption": caption,
                    "hashtags": hashtags,
                    "translation": narration,
                    "timeline": [asdict(item) for item in translated],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        checkpoint()
        db.init_db()
        db.add_pending(
            str(output_path),
            full_caption,
            (
                f"literal-translation:{source}#"
                f"{start:.3f}-{end:.3f}"
            ),
            origin_job_id=operation_id,
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        raise

    print(f"Готово: {output_path.name}")
    print("Перевод добавлен в очередь ручной проверки.")
    return output_path
