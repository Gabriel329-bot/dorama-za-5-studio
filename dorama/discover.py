"""Мультиплатформенный поиск открытых метаданных без скачивания чужого видео."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from html import unescape
from typing import Any

import requests

from job_control import checkpoint, run_process, submit_cancellable
from runtime_support import python_module_command
from settings import CONFIG

USER_AGENT = "DoramaRadar/2.0 (+local metadata search)"


@dataclass
class Trend:
    title: str
    channel: str
    url: str
    views: int
    duration: float | None = None
    source: str = "YouTube"
    description: str = ""
    license: str = "Не указана"

    @property
    def score(self) -> float:
        return math.log10(max(self.views, 1))


def clean_text(value: object) -> str:
    """Убрать HTML-разметку из названий и описаний поисковой выдачи."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", str(value or "")))).strip()


def parse_duration(value: object) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    if not all(part.strip().isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return float(total)


def query_variants(query: str) -> list[str]:
    """Добавить англо- и китайскоязычные формулировки, не теряя исходный запрос."""
    query = re.sub(r"\s+", " ", query).strip()
    variants = [query]
    lower = query.lower()
    english_parts = ["Chinese drama"]
    chinese_parts = ["中国电视剧"]
    aliases = (
        (("истор", "костюм"), "historical costume", "古装"),
        (("роман", "любов"), "romance", "爱情"),
        (("мини", "корот"), "mini short", "短剧"),
        (("триллер", "детектив"), "thriller mystery", "悬疑"),
        (("трейлер",), "trailer", "预告"),
        (("серия", "эпизод"), "episode", "全集"),
    )
    for markers, english, chinese in aliases:
        if any(marker in lower for marker in markers):
            english_parts.append(english)
            chinese_parts.append(chinese)
    if re.search(r"[а-яё]", lower):
        variants.extend((" ".join(english_parts), " ".join(chinese_parts)))
        if len(english_parts) > 1:
            variants.extend(("Chinese drama", "中国电视剧"))
    elif "chinese" not in lower and "中国" not in query:
        variants.append(f"{query} Chinese drama")
    return list(dict.fromkeys(item for item in variants if item))


def is_relevant(text: str, query: str) -> bool:
    """Отсеять очевидные совпадения по слову drama, не связанные с дорамами."""
    haystack = clean_text(text).lower()
    lower = query.lower()
    concepts: list[tuple[str, ...]] = []
    if any(marker in lower for marker in ("китай", "chinese", "china", "中国")):
        concepts.append(("китай", "chinese", "china", "中国", "华语"))
    if any(marker in lower for marker in ("дорам", "drama", "сериал", "电视剧")):
        concepts.append(("дорам", "drama", "cdrama", "c-drama", "电视剧", "短剧", "剧集"))
    if any(marker in lower for marker in ("истор", "костюм", "historical", "古装")):
        concepts.append(("истор", "historical", "costume", "period", "古装"))
    if any(marker in lower for marker in ("роман", "любов", "romance", "爱情")):
        concepts.append(("роман", "любов", "romance", "love", "爱情"))
    if not concepts:
        tokens = [token for token in re.findall(r"[a-zа-яё0-9]+", lower) if len(token) >= 4]
        return not tokens or any(token in haystack for token in tokens)
    matched = sum(any(marker in haystack for marker in markers) for markers in concepts)
    return matched >= min(2, len(concepts))


def _parse_search(payload: dict[str, Any], source: str = "YouTube") -> list[Trend]:
    trends: list[Trend] = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        video_id = item.get("id", "")
        url = item.get("webpage_url") or item.get("url") or ""
        if source == "YouTube" and video_id and not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={video_id}"
        trends.append(
            Trend(
                title=clean_text(item["title"]),
                channel=clean_text(item.get("channel") or item.get("uploader") or "неизвестный канал"),
                url=str(url),
                views=int(item.get("view_count") or 0),
                duration=parse_duration(item.get("duration")),
                source=source,
                description=clean_text(item.get("description"))[:600],
                license=clean_text(item.get("license")) or "Не указана",
            )
        )
    return sorted(trends, key=lambda item: item.score, reverse=True)


def _quota(limit: int, variants: list[str]) -> int:
    return max(3, math.ceil(limit / max(len(variants), 1)))


def search_youtube(query: str, limit: int = 10) -> list[Trend]:
    """Получить только заголовки/счётчики/ссылки; медиаконтент не скачивается."""
    items: list[Trend] = []
    variants = query_variants(query)
    for variant in variants:
        checkpoint()
        command = python_module_command(
            "yt_dlp",
            "--flat-playlist",
            "--dump-single-json",
            f"ytsearch{_quota(limit, variants)}:{variant}",
        )
        result = run_process(command, capture_output=True, text=True, timeout=90)
        if result.returncode != 0:
            continue
        try:
            items.extend(_parse_search(json.loads(result.stdout)))
        except json.JSONDecodeError:
            continue
    return _deduplicate(items, limit)


def _bilibili_video_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for group in (payload.get("data") or {}).get("result") or []:
        if group.get("result_type") == "video":
            return [item for item in group.get("data") or [] if isinstance(item, dict)]
    return []


def search_bilibili(query: str, limit: int = 10) -> list[Trend]:
    items: list[Trend] = []
    variants = query_variants(query)
    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        })
        session.get("https://www.bilibili.com/", timeout=12)
        for variant in variants:
            checkpoint()
            params: dict[str, str | int] = {"keyword": variant, "page": 1}
            response = session.get(
                "https://api.bilibili.com/x/web-interface/search/all/v2",
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                continue
            for row in _bilibili_video_rows(payload)[:_quota(limit, variants)]:
                bvid = str(row.get("bvid") or "")
                if not bvid:
                    continue
                title = clean_text(row.get("title"))
                description = clean_text(row.get("description"))[:600]
                if not is_relevant(f"{title} {description}", query):
                    continue
                items.append(Trend(
                    title=title,
                    channel=clean_text(row.get("author") or "неизвестный автор"),
                    url=f"https://www.bilibili.com/video/{bvid}",
                    views=int(row.get("play") or 0),
                    duration=parse_duration(row.get("duration")),
                    source="Bilibili",
                    description=description,
                ))
    return _deduplicate(items, limit)


def search_dailymotion(query: str, limit: int = 10) -> list[Trend]:
    items: list[Trend] = []
    variants = query_variants(query)
    for variant in variants:
        checkpoint()
        params: dict[str, str | int] = {
            "search": variant,
            "fields": "id,title,description,owner.screenname,url,views_total,duration",
            "limit": min(_quota(limit, variants), 100),
        }
        response = requests.get(
            "https://api.dailymotion.com/videos",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        for row in response.json().get("list") or []:
            url = str(row.get("url") or "")
            if not url and row.get("id"):
                url = f"https://www.dailymotion.com/video/{row['id']}"
            title = clean_text(row.get("title"))
            description = clean_text(row.get("description"))[:600]
            if not is_relevant(f"{title} {description}", query):
                continue
            items.append(Trend(
                title=title,
                channel=clean_text(row.get("owner.screenname") or "неизвестный автор"),
                url=url,
                views=int(row.get("views_total") or 0),
                duration=parse_duration(row.get("duration")),
                source="Dailymotion",
                description=description,
            ))
    return _deduplicate(items, limit)


def search_internet_archive(query: str, limit: int = 10) -> list[Trend]:
    items: list[Trend] = []
    variants = query_variants(query)
    for variant in variants:
        checkpoint()
        response = requests.get(
            "https://archive.org/advancedsearch.php",
            params=[
                ("q", f"({variant}) AND mediatype:movies"),
                ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "description"),
                ("fl[]", "creator"), ("fl[]", "downloads"), ("fl[]", "runtime"),
                ("fl[]", "licenseurl"), ("rows", _quota(limit, variants)),
                ("page", 1), ("output", "json"),
            ],
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        response.raise_for_status()
        for row in (response.json().get("response") or {}).get("docs") or []:
            identifier = str(row.get("identifier") or "")
            if not identifier:
                continue
            license_value = row.get("licenseurl")
            if isinstance(license_value, list):
                license_value = ", ".join(map(str, license_value))
            items.append(Trend(
                title=clean_text(row.get("title")),
                channel=clean_text(row.get("creator") or "Internet Archive"),
                url=f"https://archive.org/details/{identifier}",
                views=int(row.get("downloads") or 0),
                duration=parse_duration(row.get("runtime")),
                source="Internet Archive",
                description=clean_text(row.get("description"))[:600],
                license=clean_text(license_value) or "Не указана",
            ))
    return _deduplicate(items, limit)


def _metadata_value(metadata: dict[str, Any], name: str) -> str:
    value = metadata.get(name) or {}
    return clean_text(value.get("value") if isinstance(value, dict) else value)


def search_wikimedia_commons(query: str, limit: int = 10) -> list[Trend]:
    items: list[Trend] = []
    variants = query_variants(query)
    for variant in variants:
        checkpoint()
        params: dict[str, str | int] = {
            "action": "query", "generator": "search",
            "gsrsearch": f"{variant} filetype:video", "gsrnamespace": 6,
            "gsrlimit": min(_quota(limit, variants), 50),
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "format": "json", "formatversion": 2,
        }
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        response.raise_for_status()
        for page in (response.json().get("query") or {}).get("pages") or []:
            info = ((page.get("imageinfo") or [{}])[0])
            if not str(info.get("mime") or "").startswith("video/"):
                continue
            metadata = info.get("extmetadata") or {}
            items.append(Trend(
                title=_metadata_value(metadata, "ObjectName") or clean_text(page.get("title")).removeprefix("File:"),
                channel=_metadata_value(metadata, "Artist") or "Wikimedia Commons",
                url=str(info.get("descriptionurl") or ""),
                views=0,
                duration=parse_duration(info.get("duration")),
                source="Wikimedia Commons",
                description=_metadata_value(metadata, "ImageDescription")[:600],
                license=_metadata_value(metadata, "LicenseShortName") or _metadata_value(metadata, "UsageTerms") or "Не указана",
            ))
    return _deduplicate(items, limit)


def _deduplicate(items: list[Trend], limit: int | None = None) -> list[Trend]:
    unique: dict[str, Trend] = {}
    for item in items:
        key = item.url.lower().rstrip("/") or f"{item.source}:{item.title.lower()}"
        current = unique.get(key)
        if current is None or item.score > current.score:
            unique[key] = item
    ranked = sorted(unique.values(), key=lambda item: item.score, reverse=True)
    return ranked[:limit] if limit else ranked


SEARCH_PROVIDERS: dict[str, Callable[[str, int], list[Trend]]] = {
    "youtube": search_youtube,
    "bilibili": search_bilibili,
    "dailymotion": search_dailymotion,
    "internet_archive": search_internet_archive,
    "wikimedia_commons": search_wikimedia_commons,
}


def search_all_sources(query: str, limit_per_source: int = 10) -> list[Trend]:
    """Искать параллельно; ошибка одной площадки не обрывает остальные."""
    enabled = CONFIG.get("dorama", {}).get("search_sources") or list(SEARCH_PROVIDERS)
    providers = [(name, SEARCH_PROVIDERS[name]) for name in enabled if name in SEARCH_PROVIDERS]
    if not providers:
        raise RuntimeError("Не выбран ни один поддерживаемый источник поиска")
    buckets: dict[str, list[Trend]] = {}
    errors: list[str] = []
    pool = ThreadPoolExecutor(max_workers=min(len(providers), 5))
    futures = {
        submit_cancellable(pool, provider, query, limit_per_source): name
        for name, provider in providers
    }
    try:
        for future in as_completed(futures):
            checkpoint()
            name = futures[future]
            try:
                results = future.result()
                if results:
                    buckets[name] = results
                    print(f"  + {results[0].source}: {len(results)} сигналов")
                else:
                    errors.append(f"{name}: результатов нет")
            except Exception as exc:  # noqa: BLE001 — один источник не останавливает поиск
                errors.append(f"{name}: {type(exc).__name__}")
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    for error in errors:
        print(f"  ! {error}")
    # Round-robin сохраняет разнообразие: один популярный сайт не вытесняет остальные.
    combined: list[Trend] = []
    depth = max((len(items) for items in buckets.values()), default=0)
    for index in range(depth):
        for name, _ in providers:
            if index < len(buckets.get(name, [])):
                combined.append(buckets[name][index])
    unique: list[Trend] = []
    seen: set[str] = set()
    for item in combined:
        key = item.url.lower().rstrip("/") or f"{item.source}:{item.title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def trends_as_dicts(items: list[Trend]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]
