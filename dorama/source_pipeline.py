"""Лицензированная серия -> сцены -> русский пересказ -> озвучка -> ролик 5 минут."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imageio_ffmpeg  # type: ignore[import-untyped]
import requests

from clipper.render import _ass_escape_path
from clipper.transcribe import Transcript, Word, transcribe
from dorama.render import _fit_audio, create_subtitles
from dorama.script import _normalize_hashtags
from dorama.speech import synthesize
from job_control import checkpoint, ollama_generate, run_process
from settings import CONFIG, PENDING_DIR
from storage import db
from storage.files import atomic_write_text
from video_accel import selected_encoder_options


@dataclass(frozen=True)
class TimelineItem:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SourceScene:
    start: float
    end: float
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class EpisodePlan:
    title: str
    caption: str
    narration: str
    hashtags: list[str]
    scenes: list[SourceScene]


def _slug(text: str) -> str:
    value = re.sub(r"[^\wа-яА-ЯёЁ-]+", "_", text, flags=re.UNICODE).strip("_")
    return value[:52] or "licensed_dorama"


def _artifact_id() -> str:
    now = datetime.now(timezone.utc).astimezone()
    return f"{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def _video_duration(path: Path) -> float:
    result = run_process(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError("Не удалось определить длительность исходного видео")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _make_timeline(words: list[Word], bucket_seconds: int = 75, max_chars: int = 34000) -> list[TimelineItem]:
    if not words:
        return []
    buckets: dict[int, list[Word]] = {}
    for word in words:
        buckets.setdefault(int(word.start // bucket_seconds), []).append(word)
    items: list[TimelineItem] = []
    for bucket in sorted(buckets):
        current = buckets[bucket]
        text = " ".join(word.text for word in current)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 850:
            text = text[:850].rsplit(" ", 1)[0] + "…"
        items.append(TimelineItem(current[0].start, current[-1].end, text))

    encoded_lengths = [len(item.text) + 50 for item in items]
    if sum(encoded_lengths) <= max_chars:
        return items
    keep_count = max(4, int(len(items) * max_chars / sum(encoded_lengths)))
    indexes = sorted({round(index * (len(items) - 1) / max(keep_count - 1, 1)) for index in range(keep_count)})
    return [items[index] for index in indexes]


def _ask_ollama(prompt: str) -> dict[str, Any]:
    cfg = CONFIG["highlight"]
    raw = ollama_generate(
        f"{cfg['ollama_host'].rstrip('/')}/api/generate",
        {
            "model": cfg["model"],
            "prompt": prompt,
            "format": "json",
            "options": {"temperature": 0.32, "num_ctx": cfg.get("context_tokens", 16384)},
        },
        timeout=900,
    )
    return _parse_json_response(raw)


def _parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except (TypeError, json.JSONDecodeError):
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("Локальная модель вернула некорректный план пересказа")


def _fallback_scenes(duration: float, count: int) -> list[SourceScene]:
    usable = max(duration - 4, 1)
    scene_length = min(38.0, max(12.0, usable / max(count, 1) * 0.7))
    if duration <= scene_length:
        return [SourceScene(0.0, duration, "весь доступный материал")]
    result = []
    for index in range(count):
        center = (index + 0.5) * duration / count
        start = min(max(0.0, center - scene_length / 2), duration - scene_length)
        result.append(SourceScene(start, start + scene_length, "равномерная сцена"))
    return result


def _validate_scenes(raw_scenes: list[Any], duration: float, desired_count: int = 9) -> list[SourceScene]:
    result: list[SourceScene] = []
    for raw in raw_scenes or []:
        if not isinstance(raw, dict):
            continue
        try:
            start = max(0.0, float(raw.get("start", 0)))
            end = min(duration, float(raw.get("end", start)))
        except (TypeError, ValueError):
            continue
        if end - start < 6:
            continue
        if end - start > 55:
            end = start + 55
        if end <= duration:
            result.append(SourceScene(start, end, str(raw.get("reason", ""))[:180]))
    result.sort(key=lambda item: item.start)
    deduped: list[SourceScene] = []
    for scene in result:
        if deduped and scene.start < deduped[-1].end - 3:
            continue
        deduped.append(scene)
    if len(deduped) < 3:
        return _fallback_scenes(duration, desired_count)
    return deduped[: max(desired_count, 3)]


def _build_sequence(scenes: list[SourceScene], target_duration: float) -> list[SourceScene]:
    if not scenes:
        raise RuntimeError("Не выбраны сцены для монтажа")
    sequence: list[SourceScene] = []
    remaining = target_duration
    cursor = 0
    while remaining > 0.05:
        source = scenes[cursor % len(scenes)]
        take = min(source.duration, remaining)
        if take <= 0:
            raise RuntimeError("В плане есть пустая сцена")
        sequence.append(SourceScene(source.start, source.start + take, source.reason))
        remaining -= take
        cursor += 1
        if cursor > 200:
            raise RuntimeError("Не удалось собрать визуальную последовательность")
    return sequence


def _expand_narration(narration: str, timeline: list[TimelineItem], focus: str, target_words: int) -> str:
    if len(narration.split()) >= target_words:
        return narration.strip()
    prompt = f"""Перепиши и расширь русский закадровый пересказ до {target_words} слов, допустимое отклонение не больше 5 процентов.
Сохрани факты только из расшифровки. Не придумывай имена, события и финал.
Фокус: {focus or 'последовательный пересказ без спойлеров'}.
Можно подробнее связать уже упомянутые события и объяснить последовательность действий, но нельзя описывать то, чего нет в расшифровке.
Черновик ({len(narration.split())} слов): {narration}
Расшифровка по времени: {json.dumps([asdict(item) for item in timeline], ensure_ascii=False)}
Верни JSON: {{"narration":"текст"}}. Без markdown."""
    expanded = _ask_ollama(prompt).get("narration")
    return str(expanded or narration).strip()


def _create_plan(transcript: Transcript, duration: float, source_name: str, focus: str) -> EpisodePlan:
    cfg = CONFIG["episode"]
    timeline = _make_timeline(transcript.words)
    if not timeline:
        raise RuntimeError("Не удалось построить таймлайн по расшифровке")
    prompt = f"""Ты монтажный редактор русскоязычного канала о дорамах.
Материал загружен владельцем прав. По расшифровке подготовь оригинальный пересказ.
Не придумывай факты, имена, отношения или финал, которых нет в тексте.
Не копируй длинные реплики. Пиши бодрым голосом TikTok-сторителлинга.
Исходный файл: {source_name}
Длительность: {duration:.1f} секунд.
Фокус пользователя: {focus or 'ключевые события по порядку, без раскрытия финала'}
Таймлайн: {json.dumps([asdict(item) for item in timeline], ensure_ascii=False)}

Верни строгий JSON:
{{
  "title":"до 90 символов",
  "caption":"до 220 символов",
  "narration":"около {cfg['target_narration_words']} русских слов",
  "hashtags":["#тег"],
  "scenes":[{{"start":12.0,"end":45.0,"reason":"что видно или слышно"}}]
}}
Выбери {cfg['scene_count']} хронологических сцен по 15–50 секунд. Без markdown."""
    data = None
    for attempt in range(2):
        try:
            data = _ask_ollama(prompt)
            break
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            if attempt == 0:
                print(f"  Повторная попытка генерации плана… ({exc})")
                continue
            raise RuntimeError(f"Не удалось подготовить пересказ через Ollama: {exc}") from exc
    if data is None:
        raise RuntimeError("Не удалось подготовить пересказ через Ollama")

    title = str(data.get("title") or f"Короткий пересказ: {Path(source_name).stem}").strip()[:100]
    caption = str(data.get("caption") or title).strip()[:500]
    narration = str(data.get("narration") or "").strip()
    target_words = int(cfg["target_narration_words"])
    narration = narration or title
    for _ in range(3):
        if len(narration.split()) >= int(target_words * 0.88):
            break
        narration = _expand_narration(narration, timeline, focus, target_words)
    if len(narration.split()) < int(target_words * 0.82):
        raise RuntimeError(
            f"Модель подготовила только {len(narration.split())} слов из целевых {target_words}; "
            "озвучка не будет искусственно замедлена"
        )
    scenes = _validate_scenes(data.get("scenes") or [], duration, int(cfg["scene_count"]))
    hashtags = _normalize_hashtags(data.get("hashtags") or [], CONFIG["dorama"]["base_hashtags"])
    return EpisodePlan(title, caption, narration, hashtags, scenes)


def _render_montage(
    source_path: Path,
    scenes: list[SourceScene],
    narration_audio: Path,
    narration: str,
    output_path: Path,
    target_duration: float,
) -> Path:
    sequence = _build_sequence(scenes, target_duration)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path = output_path.with_suffix(".ass")
    fitted_audio = output_path.with_suffix(".voice.m4a")
    create_subtitles(narration, target_duration, subtitle_path)
    _fit_audio(narration_audio, target_duration, fitted_audio)

    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y"]
    for scene in sequence:
        command.extend(["-ss", f"{scene.start:.3f}", "-t", f"{scene.duration:.3f}", "-i", str(source_path)])
    command.extend(["-i", str(fitted_audio)])

    filters: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, _scene in enumerate(sequence):
        filters.append(
            f"[{index}:v]split=2[bg{index}][fg{index}];"
            f"[bg{index}]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,gblur=sigma=28[bgx{index}];"
            f"[fg{index}]scale=1080:1920:force_original_aspect_ratio=decrease[fgx{index}];"
            f"[bgx{index}][fgx{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,"
            f"format=yuv420p,setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={float(CONFIG['episode']['original_audio_volume']):.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
        video_labels.append(f"[v{index}]")
        audio_labels.append(f"[a{index}]")
    concat_labels = "".join(
        f"{video_labels[index]}{audio_labels[index]}" for index in range(len(sequence))
    )
    filters.append(concat_labels + f"concat=n={len(sequence)}:v=1:a=1[montage][bed]")
    filters.append(f"[montage]subtitles='{_ass_escape_path(subtitle_path)}'[video]")
    voice_index = len(sequence)
    filters.append(
        f"[bed][{voice_index}:a]amix=inputs=2:duration=first:weights='0.18 1.0'[audio]"
    )
    encoder, encoder_args = selected_encoder_options()
    print(f"  FFmpeg encoder: {encoder}")
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[video]", "-map", "[audio]",
            *encoder_args,
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-t", f"{target_duration:.3f}", str(output_path),
        ]
    )
    try:
        result = run_process(command, capture_output=True, text=True)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        subtitle_path.unlink(missing_ok=True)
        fitted_audio.unlink(missing_ok=True)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg не смог собрать пятиминутный пересказ:\n{result.stderr[-3500:]}")
    return output_path


def create_episode_recap(
    source_video: str | Path,
    focus: str = "",
    source_info: dict[str, Any] | None = None,
    progress_offset: int = 0,
) -> Path:
    cfg = CONFIG["episode"]
    source = Path(source_video)
    if not source.is_file():
        raise FileNotFoundError(f"Исходное видео не найдено: {source}")
    if source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise ValueError(f"Неподдерживаемый формат: {source.suffix}")
    selected_focus = focus.strip()
    if len(selected_focus) > 300:
        raise ValueError("Фокус пересказа не должен превышать 300 символов")
    if not 0 <= progress_offset <= 20:
        raise ValueError("Некорректное смещение прогресса")

    target_duration = float(cfg["target_duration_seconds"])
    artifact_id = _artifact_id()
    work_root = PENDING_DIR / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PENDING_DIR / f"{artifact_id}_{_slug(source.stem)}.mp4"
    audit_path = output_path.with_suffix(".plan.json")
    total_steps = 5 + progress_offset
    try:
        with tempfile.TemporaryDirectory(prefix=f"episode_{artifact_id}_", dir=work_root) as temporary:
            work_dir = Path(temporary)
            print(
                f"[{1 + progress_offset}/{total_steps}] "
                f"Анализирую длительность {source.name}..."
            )
            checkpoint()
            duration = _video_duration(source)
            if duration < 20:
                raise RuntimeError("Исходник слишком короткий: нужно хотя бы 20 секунд")

            print(
                f"[{2 + progress_offset}/{total_steps}] "
                "Распознаю речь и строю таймлайн..."
            )
            checkpoint()
            transcript = transcribe(
                str(source), language=str(cfg.get("source_language", "auto"))
            )
            print(f"  Распознано {len(transcript.words)} слов")

            print(
                f"[{3 + progress_offset}/{total_steps}] "
                "Выбираю ключевые сцены и пишу русский пересказ..."
            )
            checkpoint()
            plan = _create_plan(transcript, duration, source.name, selected_focus)
            audio_path = work_dir / "voice.mp3"
            print(f"[{4 + progress_offset}/{total_steps}] Озвучиваю: {plan.title}")
            checkpoint()
            asyncio.run(
                synthesize(
                    plan.narration,
                    audio_path,
                    CONFIG["dorama"]["voice"],
                    CONFIG["dorama"]["rate"],
                )
            )

            output_path = PENDING_DIR / f"{artifact_id}_{_slug(plan.title)}.mp4"
            audit_path = output_path.with_suffix(".plan.json")
            print(
                f"[{5 + progress_offset}/{total_steps}] "
                "Монтирую сцены, голос и субтитры..."
            )
            checkpoint()
            _render_montage(
                source,
                plan.scenes,
                audio_path,
                plan.narration,
                output_path,
                target_duration,
            )

        attribution = ""
        source_ref = f"licensed-file:{source}"
        if source_info:
            source_title = str(source_info.get("title") or source.name)
            source_channel = str(source_info.get("channel") or "Неизвестный автор")
            source_url = str(source_info.get("url") or "")
            source_license = str(source_info.get("license") or "Разрешённый источник")
            attribution = (
                f"\n\nИсточник видеоматериала: {source_title} — {source_channel}"
                f"\n{source_url}\nЛицензия: {source_license}"
            )
            source_ref = f"licensed-url:{source_url}"
        caption = f"{plan.caption}\n\n{' '.join(plan.hashtags)}{attribution}".strip()
        atomic_write_text(
            audit_path,
            json.dumps(
                {
                    "source": str(source),
                    "source_info": source_info,
                    "duration": duration,
                    "transcript_words": len(transcript.words),
                    "focus": selected_focus,
                    "title": plan.title,
                    "caption": plan.caption,
                    "hashtags": plan.hashtags,
                    "narration": plan.narration,
                    "scenes": [asdict(scene) for scene in plan.scenes],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        checkpoint()
        db.init_db()
        db.add_pending(str(output_path), caption, source_ref)
    except BaseException:
        output_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        raise

    print(f"Готово: {output_path.name}")
    print("Ролик добавлен в очередь проверки и не опубликован автоматически до следующего слота.")
    return Path(output_path)
