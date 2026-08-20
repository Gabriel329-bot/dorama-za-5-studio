"""Оригинальный сценарий и хештеги на основе найденных тренд-сигналов."""
# ruff: noqa: ISC004 — длинные абзацы намеренно собраны из соседних строк
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests

from dorama.discover import Trend, trends_as_dicts
from job_control import ollama_generate
from settings import CONFIG


@dataclass
class DoramaScript:
    title: str
    narration: str
    caption: str
    hashtags: list[str]
    sources: list[str]


CATEGORY_MARKERS = {
    "исторические и костюмные истории": ("истор", "historical", "costume", "古装"),
    "романтика и истории отношений": ("любов", "роман", "love", "romance", "marriage", "брак"),
    "мини-дорамы и короткие истории": ("мини", "корот", "mini", "short", "vertical", "微短"),
    "подборки новинок и ожидаемых премьер": ("топ", "лучши", "новин", "ожида", "top", "best", "new", "anticipated"),
}


def _grounded_narration(trends: list[Trend], query: str, target_words: int = 0) -> str:
    """Фактический текст строится из счётчиков маркеров, а не из догадок модели."""
    titles = [trend.title.lower() for trend in trends]
    counts = {
        category: sum(any(marker in title for marker in markers) for title in titles)
        for category, markers in CATEGORY_MARKERS.items()
    }
    ranked = [(name, count) for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True) if count]
    count_by_name = dict(ranked)
    historical = count_by_name.get("исторические и костюмные истории", 0)
    romance = count_by_name.get("романтика и истории отношений", 0)
    short = count_by_name.get("мини-дорамы и короткие истории", 0)
    premieres = count_by_name.get("подборки новинок и ожидаемых премьер", 0)
    source_counts = Counter(trend.source for trend in trends)
    source_summary = ", ".join(f"{name} — {count}" for name, count in source_counts.items())
    platform_word = "площадке" if len(source_counts) == 1 else "площадках"
    paragraphs = [
        f"Что сейчас заметно в поиске по теме «{query}»? Мы проверили {len(trends)} открытых заголовков и описаний "
        f"на {len(source_counts)} {platform_word}: {source_summary}. "
        "Это не рейтинг сериалов, не пересказ серий и не заявление о качестве конкретной дорамы. Такой радар "
        "показывает, какие форматы чаще используют авторы подборок и какие темы могут привлекать внимание зрителей. "
        "Сегодня разберём четыре направления, а в конце составим простой способ выбрать дораму без случайных спойлеров.",

        f"Сначала о методе. Агент не скачивает эпизоды и не принимает громкий заголовок за доказанный факт. "
        f"Он учитывает только формулировки и открытые счётчики найденных результатов. В этой выборке маркеры новинок, "
        f"ожидаемых премьер и рейтинговых подборок встретились в {premieres} из {len(trends)} заголовков. Это означает "
        "лишь одно: формат рекомендации востребован у авторов контента. Чтобы советовать конкретный сериал, затем нужно "
        "проверить официальное описание, дату выхода, доступный перевод и площадку просмотра.",

        f"Первое направление — исторические и костюмные истории. Связанные с ним слова встретились в {historical} из "
        f"{len(trends)} заголовков. Зрителю такого направления обычно важно заранее определить, чего он хочет от просмотра: "
        "политической интриги, приключения, романтической линии или спокойной истории отношений. По одному яркому ролику "
        "этого понять нельзя. Поэтому хороший обзор должен отдельно назвать жанр, темп повествования и наличие тяжёлых тем, "
        "не раскрывая ключевые повороты. Именно такой формат мы будем использовать в следующих выпусках.",

        f"Второе направление — романтика и истории отношений. Такие маркеры обнаружены в {romance} из {len(trends)} "
        "заголовков. Здесь особенно легко попасться на кликбейт: один и тот же сериал могут представить как комедию, драму "
        "или историю мести. Полезнее не пересказывать завязку, а ответить на практические вопросы. Насколько быстро развивается "
        "история, есть ли выраженная комедийная часть, насколько важны второстепенные персонажи и завершён ли сезон. Эти пункты "
        "помогают выбрать сериал лучше, чем громкое обещание неожиданного финала.",

        f"Третье направление — мини-дорамы и короткие вертикальные истории. Их признаки встретились в {short} из {len(trends)} "
        "результатов. Такой формат удобно смотреть небольшими отрезками, но короткая продолжительность серии не гарантирует "
        "быстрого или качественного сюжета. При выборе стоит проверить общее количество эпизодов, среднюю длину серии и наличие "
        "официальных субтитров. Ещё важно отличать легальную публикацию от канала, который просто перезалил чужой материал. "
        "В нашем проекте такие перезаливы не используются.",

        "Четвёртое направление — большие подборки новинок. Они хорошо работают как карта, но плохо подходят как окончательный "
        "совет. Если в одном видео перечислено десять или пятнадцать сериалов, на каждый обычно остаётся слишком мало времени. "
        "Поэтому агент будет использовать такие подборки только как сигнал для дальнейшей проверки. После этого отдельный выпуск "
        "сможет разобрать одну дораму или один жанр: без чужих сцен, с оригинальной русской озвучкой, понятными критериями и "
        "ссылками на источники, по которым проверялись сведения.",

        "Как выбрать направление уже сейчас? Сначала решите, нужен ли вам современный или исторический сеттинг. Затем выберите "
        "темп: медленное развитие отношений, динамичная интрига или короткие серии. После этого проверьте, завершён ли показ и "
        "существует ли перевод на понятном вам языке. И только потом смотрите отзывы без спойлеров. Такой порядок экономит время "
        "и помогает не начинать длинный сериал только из-за красивого фрагмента, вырванного из контекста.",

        "Итог этого выпуска простой: поисковый тренд помогает увидеть интерес аудитории, но не заменяет проверку фактов и личный "
        "вкус. Следующие ролики можно посвятить историческим дорамам, современной романтике или мини-дорамам. Напишите, какое "
        "направление разобрать первым. Мы соберём официальные описания, сравним доступные сведения и подготовим отдельный обзор "
        "без спойлеров и без использования украденных эпизодов."
    ]
    target_floor = max(45, int(target_words * 0.90))
    for index, trend in enumerate(trends, start=1):
        if len(" ".join(paragraphs).split()) >= target_floor:
            break
        safe_title = re.sub(r"\s+", " ", trend.title).strip()[:120]
        view_note = f", открытый счётчик просмотров — {trend.views}" if trend.views > 0 else ""
        duration_note = f", длительность результата — около {round(trend.duration / 60)} минут" if trend.duration else ""
        paragraphs.append(
            f"Поисковый сигнал номер {index}: на площадке {trend.source} найден материал с заголовком «{safe_title}»"
            f"{view_note}{duration_note}. Мы не считаем этот заголовок подтверждением сюжета или качества сериала. "
            "Он нужен только для сравнения интереса между площадками; перед рекомендацией сведения проверяются по официальному описанию."
        )
    return "\n\n".join(paragraphs)


def _normalize_hashtags(values: list[Any], base: list[str]) -> list[str]:
    result: list[str] = []
    for raw in [*base, *values]:
        tag = re.sub(r"[^\wа-яА-ЯёЁ]", "", str(raw).lstrip("#"), flags=re.UNICODE)
        if tag and f"#{tag.lower()}" not in {item.lower() for item in result}:
            result.append(f"#{tag}")
    return result[:10]


def _ask_ollama(prompt: str, host: str, model: str) -> dict[str, Any]:
    raw = ollama_generate(
        f"{host.rstrip('/')}/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "format": "json",
            "options": {"temperature": 0.45, "num_ctx": 8192},
        },
        timeout=600,
    )
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("Ollama вернула JSON не в виде объекта")
    return payload


def _ensure_narration_length(text: str) -> str:
    """Qwen 7B иногда игнорирует лимит слов; безопасно дополняем только наблюдениями о выдаче."""
    if len(text.split()) >= 45:
        return text
    addition = (
        "В самой поисковой выдаче сейчас одновременно заметны подборки новинок, "
        "исторические романтические истории и короткие вертикальные драмы с резкими поворотами. "
        "Это не рейтинг качества, а только сигнал того, какие форматы чаще попадают в обсуждение. "
        "Перед просмотром лучше проверить официальное описание и доступность перевода. "
        "Какой формат разобрать следующим: исторический, современный или мини-дорамы?"
    )
    return f"{text} {addition}".strip()


def create_script(trends: list[Trend], query: str) -> DoramaScript:
    if not trends:
        raise RuntimeError("По запросу не найдено трендов для сценария")

    cfg = CONFIG["dorama"]
    hcfg = CONFIG["highlight"]
    sources = trends_as_dicts(trends)
    prompt = f"""Ты редактор русскоязычного канала о китайских дорамах.
На основе ТОЛЬКО приведённых заголовков и метаданных сделай оригинальный короткий ролик.
Не пересказывай сюжет, актёров, даты и рейтинги, если этих фактов нет в данных.
Не говори, что просмотрел сериал. Можно описывать наблюдаемые тренды и типы историй.
Тема поиска: {query}
Данные: {json.dumps(sources, ensure_ascii=False)}

Нужно вернуть строгий JSON:
{{"title":"до 70 символов","narration":"краткая подводка","caption":"до 180 символов","hashtags":["#тег"],"source_urls":["URL"]}}
Не используй markdown. Не копируй длинные фрагменты заголовков дословно.
"""
    try:
        data = _ask_ollama(prompt, hcfg["ollama_host"], hcfg["model"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Не удалось создать сценарий через Ollama: {exc}") from exc

    title = f"Дорама-радар: {query}"[:70]
    narration = _grounded_narration(trends, query, int(cfg.get("target_script_words", 680)))
    caption = f"Радар интереса по теме «{query}»: направления, которые чаще встречаются в свежей выдаче."[:180]
    if len(narration.split()) < 45:
        raise RuntimeError("Не удалось сформировать достаточно длинный сценарий")
    hashtags = _normalize_hashtags(data.get("hashtags") or [], cfg["base_hashtags"])
    valid_urls = {trend.url for trend in trends}
    selected_sources = [url for url in data.get("source_urls") or [] if url in valid_urls]
    if not selected_sources:
        selected_sources = [trend.url for trend in trends[:3]]
    return DoramaScript(title, narration, caption, hashtags, selected_sources[:5])
