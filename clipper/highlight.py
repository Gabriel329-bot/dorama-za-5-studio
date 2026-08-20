"""Отбор лучших отрезков для Reels/Shorts из транскрипта через локальную модель (Ollama)."""
import json
from dataclasses import dataclass
from typing import Any, cast

import requests

from clipper.transcribe import Transcript
from job_control import ollama_generate
from settings import CONFIG

WORDS_PER_LINE = 8
MAX_OVERLAP_RATIO = 0.5  # доля пересечения, выше которой отрезок считается дублем уже выбранного


@dataclass
class Highlight:
    start: float
    end: float
    caption: str


def _format_timestamped_transcript(transcript: Transcript) -> str:
    lines = []
    words = transcript.words
    for i in range(0, len(words), WORDS_PER_LINE):
        chunk = words[i : i + WORDS_PER_LINE]
        line_text = " ".join(w.text for w in chunk)
        lines.append(f"[{chunk[0].start:.1f}] {line_text}")
    return "\n".join(lines)


def pick_highlights(transcript: Transcript) -> list[Highlight]:
    cfg = CONFIG["highlight"]
    host = cfg["ollama_host"]
    timestamped = _format_timestamped_transcript(transcript)

    prompt = f"""Ниже — транскрипт видео на русском языке. Каждая строка начинается с таймкода
в секундах от начала видео в квадратных скобках, за ним идёт кусок речи.

Твоя задача: выбрать до {cfg['max_segments_per_video']} лучших отрывков для коротких
вертикальных видео (Reels/Shorts). Критерии хорошего отрывка:
- Длительность от {cfg['min_segment_seconds']} до {cfg['max_segment_seconds']} секунд.
- Сильное, цепляющее начало (не с середины мысли).
- Законченная мысль или история внутри отрывка.
- Не должно быть просто пересказом/анонсом того, что будет дальше — отрывок должен работать сам по себе.

Транскрипт:
{timestamped}

Ответь СТРОГО в формате JSON-объекта (без пояснений, без markdown), например:
{{"highlights": [{{"start": 12.3, "end": 45.6, "caption": "короткая цепляющая подпись к посту, до 15 слов"}}]}}

Если в видео нет ни одного отрывка, подходящего под критерии — верни {{"highlights": []}}.
"""

    try:
        raw_text = ollama_generate(
            f"{host}/api/generate",
            {
                "model": cfg["model"],
                "prompt": prompt,
                "format": "json",
                # num_ctx обязателен: по умолчанию Ollama режет контекст до 2048 токенов,
                # и транскрипт длинного видео молча обрезался бы до первых минут.
                "options": {"temperature": 0.3, "num_ctx": cfg["context_tokens"]},
            },
            timeout=600,
        )
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Не удалось достучаться до Ollama на {host}. "
            f"Убедись, что Ollama запущена (значок в трее или команда `ollama serve`) "
            f"и модель `{cfg['model']}` скачана (`ollama pull {cfg['model']}`)."
        ) from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(
            "Ollama не ответила за 10 минут. Уменьши highlight.context_tokens, "
            "выбери qwen2.5:3b или проверь нагрузку на компьютер."
        ) from e
    except requests.exceptions.HTTPError as e:
        body = e.response.text[-1000:] if e.response is not None else str(e)
        raise RuntimeError(f"Ollama вернула HTTP-ошибку: {body}") from e

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Не удалось разобрать ответ модели как JSON ({len(raw_text)} символов)"
        ) from e

    items = _unwrap_items(data)
    if items is None:
        raise RuntimeError("В ответе модели нет списка отрывков")

    video_end = transcript.words[-1].end
    return _validate(items, video_end, cfg)


def _unwrap_items(data: object) -> list[Any] | None:
    """Локальные модели непредсказуемы в обёртке: массив, {"highlights": [...]}, или иной ключ."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("highlights"), list):
            return cast(list[Any], data["highlights"])
        # запасной вариант: единственное значение-список под любым другим ключом
        list_values = [v for v in data.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
    return None


def _fit_duration(
    start: float, end: float, video_end: float, min_len: float, max_len: float
) -> tuple[float, float]:
    """Небольшие модели регулярно указывают слишком узкое окно, хотя место выбрано верно.
    Расширяем такой отрезок вокруг его центра до минимальной длины (в пределах видео),
    а слишком длинный — обрезаем с конца."""
    duration = end - start

    if duration > max_len:
        return start, start + max_len

    if duration < min_len:
        missing = min_len - duration
        start = max(0.0, start - missing / 2)
        end = min(video_end, start + min_len)
        # если упёрлись в конец видео — добираем недостающее слева
        if end - start < min_len:
            start = max(0.0, end - min_len)

    return start, end


def _validate(items: list[Any], video_end: float, cfg: dict[str, Any]) -> list[Highlight]:
    """Отбрасывает отрезки, которые модель выдумала: неверные типы, нулевую/чрезмерную
    длительность, выход за пределы видео. Слабая локальная модель ошибается регулярно."""
    min_len = cfg["min_segment_seconds"]
    max_len = cfg["max_segment_seconds"]

    highlights: list[Highlight] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            print(f"  ! пропускаю отрывок с некорректными таймкодами: {item!r}")
            continue
        caption = str(item.get("caption", "")).strip()

        start = max(0.0, start)
        end = min(end, video_end)
        if end <= start:
            print(f"  ! пропускаю отрывок с пустой длительностью: {item!r}")
            continue

        start, end = _fit_duration(start, end, video_end, min_len, max_len)
        duration = end - start
        if duration < min_len:
            print(f"  ! пропускаю отрывок {start:.0f}-{end:.0f}с: даже после расширения "
                  f"длительность {duration:.0f}с меньше {min_len}с")
            continue

        if not caption:
            caption = "Смотри до конца"

        if _overlaps_existing(start, end, highlights):
            print(f"  ! пропускаю отрывок {start:.0f}-{end:.0f}с: сильно перекрывается с уже выбранным")
            continue

        highlights.append(Highlight(start=start, end=end, caption=caption))

    return highlights[: cfg["max_segments_per_video"]]


def _overlaps_existing(start: float, end: float, chosen: list["Highlight"]) -> bool:
    """После расширения близкие отрезки склеиваются в почти одинаковые клипы — не постим дубли."""
    for h in chosen:
        overlap = min(end, h.end) - max(start, h.start)
        if overlap > 0 and overlap / min(end - start, h.end - h.start) > MAX_OVERLAP_RATIO:
            return True
    return False
