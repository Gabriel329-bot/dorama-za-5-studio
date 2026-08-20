"""Лицензированная серия -> сцены -> русский пересказ -> озвучка -> ролик 5 минут."""
from __future__ import annotations

import asyncio
import hashlib
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
from clipper.transcribe import Transcript, Word, release_models, transcribe
from dorama.render import _fit_audio, create_subtitles
from dorama.script import _normalize_hashtags
from dorama.speech import synthesize
from job_control import checkpoint, ollama_generate, run_process
from media_pipeline.audio import (
    build_montage_bed,
    prepare_background_bed,
    prepare_voice,
    render_ducked_mix,
    validate_final_media,
)
from media_pipeline.resources import render_slot, unload_ollama_model
from settings import CACHE_DIR, CONFIG, PENDING_DIR
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


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "narration": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "reason"],
            },
        },
    },
    "required": ["title", "caption", "narration", "hashtags", "scenes"],
}

_NARRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"narration": {"type": "string"}},
    "required": ["narration"],
}

_NARRATION_ADDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"addition": {"type": "string"}},
    "required": ["addition"],
}

_TIMELINE_TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    },
    "required": ["items"],
}

_JSON_WRAPPER_KEYS = ("plan", "result", "response", "data", "output")
_RUSSIAN_EDITOR_SYSTEM = (
    "Ты профессиональный русскоязычный редактор. "
    "Все текстовые значения в ответе пиши только на естественном русском языке "
    "кириллицей, даже если исходная расшифровка на китайском. "
    "Переводи только факты из расшифровки, ничего не выдумывай. "
    "重要要求：所有文本值必须翻译成自然的俄语，只能用俄语西里尔字母回答，"
    "不要复述中文原文。"
    "Возвращай только JSON по переданной схеме, без markdown и пояснений."
)


def _load_plan_checkpoint(
    path: Path,
) -> tuple[EpisodePlan, int] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_plan = data["plan"]
        plan = EpisodePlan(
            title=str(raw_plan["title"]),
            caption=str(raw_plan["caption"]),
            narration=str(raw_plan["narration"]),
            hashtags=[str(item) for item in raw_plan["hashtags"]],
            scenes=[
                SourceScene(
                    start=float(item["start"]),
                    end=float(item["end"]),
                    reason=str(item.get("reason") or ""),
                )
                for item in raw_plan["scenes"]
            ],
        )
        return plan, int(data.get("transcript_words") or 0)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


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


def _ask_ollama(
    prompt: str,
    *,
    schema: dict[str, Any],
    temperature: float = 0.24,
) -> dict[str, Any]:
    cfg = CONFIG["highlight"]
    raw = ollama_generate(
        f"{cfg['ollama_host'].rstrip('/')}/api/generate",
        {
            "model": cfg["model"],
            "system": _RUSSIAN_EDITOR_SYSTEM,
            "prompt": prompt,
            "format": schema,
            "options": {
                "temperature": temperature,
                "num_ctx": cfg.get("context_tokens", 16384),
                # A five-minute Russian narration can exceed Ollama's smaller
                # model defaults. A truncated object cannot be repaired safely.
                "num_predict": 4096,
            },
        },
        timeout=900,
    )
    return _parse_json_response(raw)


def _russian_word_count(text: str) -> int:
    return len(re.findall(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", text))


def _require_russian_narration(data: dict[str, Any]) -> None:
    narration = str(data.get("narration") or "").strip()
    if _russian_word_count(narration) < 12:
        raise ValueError(
            "модель вернула пересказ не на русском языке или слишком короткий текст"
        )


def _align_translation(
    timeline: list[TimelineItem], texts: list[str]
) -> list[TimelineItem]:
    if len(texts) == len(timeline):
        return [
            TimelineItem(source.start, source.end, text)
            for source, text in zip(timeline, texts, strict=True)
        ]

    words = " ".join(texts).split()
    if len(words) < len(timeline):
        raise ValueError("перевод слишком короткий для временных сегментов")

    weights = [max(len(item.text), 1) for item in timeline]
    total_weight = sum(weights)
    result: list[TimelineItem] = []
    cursor = 0
    cumulative_weight = 0
    for index, (source, weight) in enumerate(
        zip(timeline, weights, strict=True)
    ):
        cumulative_weight += weight
        remaining_segments = len(timeline) - index - 1
        if remaining_segments == 0:
            end = len(words)
        else:
            proportional_end = round(
                len(words) * cumulative_weight / total_weight
            )
            end = max(
                cursor + 1,
                min(proportional_end, len(words) - remaining_segments),
            )
        result.append(
            TimelineItem(source.start, source.end, " ".join(words[cursor:end]))
        )
        cursor = end
    return result


def _translation_cache_path(timeline: list[TimelineItem]) -> Path:
    serialized = json.dumps(
        [asdict(item) for item in timeline],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return Path(CACHE_DIR) / "translations" / f"{digest}.json"


def _load_translation_cache(
    path: Path, timeline: list[TimelineItem]
) -> list[TimelineItem] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        texts = [str(item) for item in data["texts"]]
        translated = _align_translation(timeline, texts)
        if _russian_word_count(" ".join(item.text for item in translated)) < 12:
            raise ValueError("кеш перевода не содержит русского текста")
        return translated
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def _translate_timeline(timeline: list[TimelineItem]) -> list[TimelineItem]:
    source_text = " ".join(item.text for item in timeline)
    if _russian_word_count(source_text) >= 12:
        return timeline

    cache_path = _translation_cache_path(timeline)
    cached = _load_translation_cache(cache_path, timeline)
    if cached is not None:
        print("  Перевод таймлайна: использую сохранённый кеш")
        return cached

    prompt = f"""КРИТИЧЕСКОЕ ТРЕБОВАНИЕ: переведи каждый item.text на естественный русский язык кириллицей. Не возвращай китайский текст.
重要：把每个 item.text 翻译成自然的俄语。只能输出俄语西里尔字母，不要输出中文原文。
Сохрани порядок и точное количество элементов. Не объединяй и не пропускай элементы.
Передавай только смысл исходника: не добавляй новые имена, места, даты, события или оценки.
Таймкоды возвращать не нужно — они будут восстановлены программой.
Исходный таймлайн: {json.dumps([asdict(item) for item in timeline], ensure_ascii=False)}
Верни JSON по схеме с массивом items, в каждом элементе только поле text."""

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            data = _ask_ollama(
                prompt,
                schema=_TIMELINE_TRANSLATION_SCHEMA,
                temperature=0.0,
            )
            raw_items = data.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("перевод таймлайна не содержит сегментов")
            texts = [
                str(item.get("text") or "").strip()
                if isinstance(item, dict)
                else ""
                for item in raw_items
            ]
            if any(not text for text in texts):
                raise ValueError("перевод таймлайна содержит пустой сегмент")
            if _russian_word_count(" ".join(texts)) < 12:
                raise ValueError("перевод таймлайна вернулся не на русском языке")
            if len(texts) != len(timeline):
                print(
                    "  Количество абзацев перевода скорректировано "
                    "по исходным таймкодам"
                )
            translated = _align_translation(timeline, texts)
            atomic_write_text(
                cache_path,
                json.dumps(
                    {"version": 1, "texts": [item.text for item in translated]},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return translated
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                print(
                    "  Перевод таймлайна не прошёл проверку; "
                    f"повторяю ({attempt + 2}/3)… ({exc})"
                )
    raise RuntimeError(
        "Не удалось перевести расшифровку на русский после 3 попыток. "
        f"Последняя ошибка: {last_error}"
    )


def _json_object(value: Any, depth: int = 0) -> dict[str, Any] | None:
    """Recover an object from harmless wrappers without guessing broken JSON."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key in _JSON_WRAPPER_KEYS:
            if key in value and len(value) == 1:
                nested = _json_object(value[key], depth + 1)
                if nested is not None:
                    return nested
        return value
    if isinstance(value, list) and len(value) == 1:
        return _json_object(value[0], depth + 1)
    if isinstance(value, str):
        nested_text = value.strip()
        if not nested_text:
            return None
        try:
            return _json_object(json.loads(nested_text), depth + 1)
        except (TypeError, json.JSONDecodeError):
            return None
    return None


def _parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
        recovered = _json_object(payload)
        if recovered is not None:
            return recovered
    except (TypeError, json.JSONDecodeError):
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character not in "{[\"":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except (TypeError, json.JSONDecodeError):
            continue
        recovered = _json_object(value)
        if recovered is not None:
            return recovered
    raise RuntimeError(
        "Локальная модель вернула повреждённый JSON "
        f"({len(cleaned)} символов)"
    )


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
    current_words = _russian_word_count(narration)
    if current_words >= target_words:
        return narration.strip()
    prompt = f"""Перепиши и расширь русский закадровый пересказ до {target_words} слов, допустимое отклонение не больше 5 процентов.
Сохрани факты только из расшифровки. Не придумывай имена, события и финал.
Весь результат должен быть на русском языке кириллицей. Китайскую расшифровку не повторяй — переведи её смысл на русский.
Фокус: {focus or 'последовательный пересказ без спойлеров'}.
Можно подробнее связать уже упомянутые события и объяснить последовательность действий, но нельзя описывать то, чего нет в расшифровке.
Черновик ({current_words} русских слов): {narration}
Расшифровка по времени: {json.dumps([asdict(item) for item in timeline], ensure_ascii=False)}
Верни JSON: {{"narration":"текст"}}. Без markdown."""
    expanded = _ask_ollama(
        prompt,
        schema=_NARRATION_SCHEMA,
        temperature=0.18,
    ).get("narration")
    return str(expanded or narration).strip()


def _extend_narration(
    narration: str,
    timeline: list[TimelineItem],
    focus: str,
    additional_words: int,
) -> str:
    prompt = f"""Напиши дополнительный фрагмент русского закадрового пересказа объёмом от {additional_words} до {additional_words + 40} слов.
Не завершай ответ раньше минимального объёма {additional_words} слов.
Он должен естественно продолжать существующий текст, не повторяя его формулировки.
Используй только факты из таймлайна. Не добавляй новые имена, события, отношения или финал.
Язык — только естественный русский, кириллицей.
Фокус: {focus or 'последовательный пересказ без спойлеров'}.
Существующий пересказ: {narration}
Таймлайн: {json.dumps([asdict(item) for item in timeline], ensure_ascii=False)}
Верни JSON: {{"addition":"только новый дополнительный фрагмент"}}."""
    addition = _ask_ollama(
        prompt,
        schema=_NARRATION_ADDITION_SCHEMA,
        temperature=0.12,
    ).get("addition")
    return str(addition or "").strip()


def _grow_narration(
    narration: str,
    timeline: list[TimelineItem],
    focus: str,
    target_words: int,
) -> str:
    """Grow narration monotonically; a shorter model response is never accepted."""
    best = narration.strip()
    best_words = _russian_word_count(best)
    minimum_words = int(target_words * 0.88)
    if best_words >= minimum_words:
        return best

    try:
        expanded = _expand_narration(best, timeline, focus, target_words)
    except (RuntimeError, requests.RequestException, ValueError) as exc:
        print(f"  Полное расширение не прошло проверку: {exc}")
    else:
        expanded_words = _russian_word_count(expanded)
        if expanded_words > best_words:
            best = expanded
            best_words = expanded_words
        else:
            print(
                "  Модель вернула более короткий вариант; "
                "сохраняю предыдущий черновик"
            )

    max_additions = 12
    for attempt in range(max_additions):
        if best_words >= minimum_words:
            break
        missing_words = minimum_words - best_words
        requested_words = min(220, max(100, missing_words + 30))
        try:
            addition = _extend_narration(
                best,
                timeline,
                focus,
                requested_words,
            )
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            print(
                f"  Дополнение {attempt + 1}/{max_additions} не прошло проверку; "
                f"повторяю… ({exc})"
            )
            continue
        addition_words = _russian_word_count(addition)
        if addition_words < 8:
            print(
                f"  Дополнение {attempt + 1}/{max_additions} слишком короткое; "
                "повторяю…"
            )
            continue
        candidate = f"{best.rstrip()}\n\n{addition}"
        candidate_words = _russian_word_count(candidate)
        if candidate_words <= best_words:
            continue
        best = candidate
        best_words = candidate_words
        print(
            f"  Длина пересказа: {best_words}/{target_words} русских слов"
        )
    return best


def _create_plan(transcript: Transcript, duration: float, source_name: str, focus: str) -> EpisodePlan:
    cfg = CONFIG["episode"]
    timeline = _make_timeline(transcript.words)
    if not timeline:
        raise RuntimeError("Не удалось построить таймлайн по расшифровке")
    if _russian_word_count(" ".join(item.text for item in timeline)) < 12:
        print("  Перевожу исходную расшифровку на русский с сохранением таймкодов…")
    planning_timeline = _translate_timeline(timeline)
    prompt = f"""Ты монтажный редактор русскоязычного канала о дорамах.
Материал загружен владельцем прав. По расшифровке подготовь оригинальный пересказ.
Не придумывай факты, имена, отношения или финал, которых нет в тексте.
Не копируй длинные реплики. Пиши бодрым голосом TikTok-сторителлинга.
Все текстовые поля заполни только на русском языке. Китайскую речь переводи на русский, а не повторяй иероглифами.
Исходный файл: {source_name}
Длительность: {duration:.1f} секунд.
Фокус пользователя: {focus or 'ключевые события по порядку, без раскрытия финала'}
Таймлайн: {json.dumps([asdict(item) for item in planning_timeline], ensure_ascii=False)}

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
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            data = _ask_ollama(
                prompt,
                schema=_PLAN_SCHEMA,
                temperature=0.24 if attempt == 0 else 0.12,
            )
            _require_russian_narration(data)
            break
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                print(
                    "  Ответ Ollama не прошёл проверку; "
                    f"повторяю со строгим режимом ({attempt + 2}/3)… ({exc})"
                )
                continue
            raise RuntimeError(
                "Не удалось подготовить корректный пересказ через Ollama "
                "после 3 попыток. Исходник и расшифровка сохранены; "
                f"задачу можно запустить повторно. Последняя ошибка: {exc}"
            ) from exc
    if data is None:
        raise RuntimeError(
            f"Не удалось подготовить пересказ через Ollama: {last_error}"
        )

    title = str(data.get("title") or f"Короткий пересказ: {Path(source_name).stem}").strip()[:100]
    caption = str(data.get("caption") or title).strip()[:500]
    narration = str(data.get("narration") or "").strip()
    target_words = int(cfg["target_narration_words"])
    narration = narration or title
    narration = _grow_narration(
        narration,
        planning_timeline,
        focus,
        target_words,
    )
    narration_words = _russian_word_count(narration)
    if narration_words < int(target_words * 0.82):
        raise RuntimeError(
            f"Модель подготовила только {narration_words} русских слов из целевых {target_words}; "
            "озвучка не будет искусственно замедлена"
        )
    scenes = _validate_scenes(data.get("scenes") or [], duration, int(cfg["scene_count"]))
    hashtags = _normalize_hashtags(data.get("hashtags") or [], CONFIG["dorama"]["base_hashtags"])
    return EpisodePlan(title, caption, narration, hashtags, scenes)


def _render_montage(
    source_path: Path,
    scenes: list[SourceScene],
    narration_audio: Path,
    output_path: Path,
    target_duration: float,
    work_dir: Path,
) -> Path:
    sequence = _build_sequence(scenes, target_duration)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path = output_path.with_suffix(".ass")
    fitted_audio = work_dir / "voice-fitted.wav"
    original_bed = work_dir / "montage-original.wav"

    build_montage_bed(
        source_path,
        sequence,
        original_bed,
        target_duration,
    )
    background = prepare_background_bed(
        original_bed,
        work_dir,
        duration=target_duration,
    )
    _fit_audio(narration_audio, target_duration, fitted_audio)
    voice = prepare_voice(
        fitted_audio,
        work_dir,
        duration=target_duration,
    )
    final_audio = render_ducked_mix(
        background,
        voice,
        work_dir,
        duration=target_duration,
    )
    scene_cuts: list[float] = []
    scene_cursor = 0.0
    for scene in sequence[:-1]:
        scene_cursor += scene.duration
        scene_cuts.append(scene_cursor)
    create_subtitles(
        voice,
        target_duration,
        subtitle_path,
        scene_cuts=scene_cuts,
    )

    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y"]
    for scene in sequence:
        command.extend(["-ss", f"{scene.start:.3f}", "-t", f"{scene.duration:.3f}", "-i", str(source_path)])
    audio_index = len(sequence)
    command.extend(["-i", str(final_audio)])

    filters: list[str] = []
    video_labels: list[str] = []
    for index, _scene in enumerate(sequence):
        filters.append(
            f"[{index}:v]split=2[bg{index}][fg{index}];"
            f"[bg{index}]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,gblur=sigma=28[bgx{index}];"
            f"[fg{index}]scale=1080:1920:force_original_aspect_ratio=decrease[fgx{index}];"
            f"[bgx{index}][fgx{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,"
            f"format=yuv420p,setpts=PTS-STARTPTS[v{index}]"
        )
        video_labels.append(f"[v{index}]")
    filters.append(
        "".join(video_labels)
        + f"concat=n={len(sequence)}:v=1:a=0[montage]"
    )
    filters.append(f"[montage]subtitles='{_ass_escape_path(subtitle_path)}'[video]")
    encoder, encoder_args = selected_encoder_options()
    print(f"  FFmpeg encoder: {encoder}")
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[video]", "-map", f"{audio_index}:a:0",
            *encoder_args,
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-t", f"{target_duration:.3f}", str(output_path),
        ]
    )
    try:
        with render_slot(encoder):
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
    try:
        validate_final_media(
            output_path,
            target_duration=target_duration,
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def create_episode_recap(
    source_video: str | Path,
    focus: str = "",
    source_info: dict[str, Any] | None = None,
    progress_offset: int = 0,
    operation_id: str | None = None,
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
    artifact_id = operation_id or _artifact_id()
    work_root = PENDING_DIR / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = (
        work_root / f"{operation_id}.episode-plan.json"
        if operation_id
        else None
    )
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

            saved_plan = (
                _load_plan_checkpoint(checkpoint_path)
                if checkpoint_path
                else None
            )
            if saved_plan is None:
                print(
                    f"[{2 + progress_offset}/{total_steps}] "
                    "Распознаю речь и строю таймлайн..."
                )
                checkpoint()
                transcript = transcribe(
                    str(source),
                    language=str(cfg.get("source_language", "auto")),
                )
                release_models()
                transcript_word_count = len(transcript.words)
                print(f"  Распознано {transcript_word_count} слов")

                print(
                    f"[{3 + progress_offset}/{total_steps}] "
                    "Выбираю ключевые сцены и пишу русский пересказ..."
                )
                checkpoint()
                plan = _create_plan(
                    transcript,
                    duration,
                    source.name,
                    selected_focus,
                )
                if checkpoint_path:
                    atomic_write_text(
                        checkpoint_path,
                        json.dumps(
                            {
                                "transcript_words": transcript_word_count,
                                "plan": {
                                    **asdict(plan),
                                    "scenes": [
                                        asdict(scene)
                                        for scene in plan.scenes
                                    ],
                                },
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
            else:
                plan, transcript_word_count = saved_plan
                print(
                    f"[{2 + progress_offset}/{total_steps}] "
                    "↻ Расшифровка восстановлена из контрольной точки"
                )
                print(
                    f"[{3 + progress_offset}/{total_steps}] "
                    "↻ План пересказа восстановлен"
                )
            audio_path = work_dir / "voice.mp3"
            print(f"[{4 + progress_offset}/{total_steps}] Озвучиваю: {plan.title}")
            checkpoint()
            release_models()
            unload_ollama_model()
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
                output_path,
                target_duration,
                work_dir,
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
                    "transcript_words": transcript_word_count,
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
        db.add_pending(
            str(output_path),
            caption,
            source_ref,
            origin_job_id=operation_id,
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        raise

    if checkpoint_path:
        checkpoint_path.unlink(missing_ok=True)
    print(f"Готово: {output_path.name}")
    print("Ролик добавлен в очередь проверки и не опубликован автоматически до следующего слота.")
    return Path(output_path)
