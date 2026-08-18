"""Поиск и загрузка только источников с проверяемым разрешением на переработку."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import re
import subprocess
import sys
from urllib.parse import quote, urlparse

import imageio_ffmpeg
import requests

from dorama.discover import USER_AGENT, clean_text, parse_duration, query_variants
from dorama.source_pipeline import create_episode_recap
from job_control import JobCancelled, checkpoint, run_process
from settings import CONFIG, INPUT_DIR


@dataclass(frozen=True)
class LicensedCandidate:
    video_id: str
    title: str
    channel: str
    channel_id: str
    url: str
    duration: float
    views: int
    license: str
    permission_basis: str
    description: str = ""
    source: str = "YouTube"
    download_url: str = ""
    size_bytes: int = 0


def _run_yt_dlp(arguments: list[str], timeout: int = 180) -> str:
    command = [sys.executable, "-m", "yt_dlp", *arguments]
    result = run_process(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:] or "yt-dlp завершился с ошибкой")
    return result.stdout.strip()


def _open_license_basis(license_name: str, license_url: str = "") -> str | None:
    """Разрешать только лицензии, совместимые с нарезкой и возможной монетизацией."""
    value = f"{license_name} {license_url}".lower().replace("_", "-")
    if any(marker in value for marker in ("by-nc", "/nc/", "noncommercial", "by-nd", "/nd/", "no derivatives")):
        return None
    if any(marker in value for marker in ("public domain", "publicdomain/mark", "publicdomain/zero", "cc0")):
        return "public-domain-or-cc0"
    if any(marker in value for marker in ("cc by-sa", "/licenses/by-sa/", "attribution-sharealike")):
        return "creative-commons-by-sa"
    if any(marker in value for marker in ("cc by ", "cc-by-", "/licenses/by/", "creative commons attribution")):
        return "creative-commons-by"
    return None


def _permission_basis(metadata: dict) -> str | None:
    cfg = CONFIG["licensed_sources"]
    license_name = str(metadata.get("license") or "").strip()
    if "creative commons" in license_name.lower() and "reuse allowed" in license_name.lower():
        return "youtube-creative-commons"
    channel_id = str(metadata.get("channel_id") or "")
    approved = cfg.get("approved_channels") or {}
    permission_note = str(approved.get(channel_id) or "").strip()
    if permission_note:
        return f"approved-channel:{permission_note}"
    return None


def _candidate_from_metadata(metadata: dict) -> LicensedCandidate | None:
    basis = _permission_basis(metadata)
    if not basis or metadata.get("availability") not in {None, "public"}:
        return None
    video_id = str(metadata.get("id") or "")
    url = str(metadata.get("webpage_url") or metadata.get("original_url") or "")
    if not video_id or not url.startswith("http"):
        return None
    return LicensedCandidate(
        video_id=video_id,
        title=clean_text(metadata.get("title")) or "Без названия",
        channel=clean_text(metadata.get("channel") or metadata.get("uploader") or "Неизвестный автор"),
        channel_id=str(metadata.get("channel_id") or ""),
        url=url,
        duration=float(metadata.get("duration") or 0),
        views=int(metadata.get("view_count") or 0),
        license=str(metadata.get("license") or "Разрешение из белого списка"),
        permission_basis=basis,
        description=clean_text(metadata.get("description"))[:1200],
        source="YouTube",
        download_url=url,
    )


def _query_tokens(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zа-яё0-9]+", query.lower())
        if len(token) >= 3
    }


def _relevance_score(candidate: LicensedCandidate, query: str) -> float:
    haystack = f"{candidate.title} {candidate.description}".lower()
    aliases = {
        "дорам": ("дорам", "drama", "cdrama", "c-drama", "电视剧", "短剧"),
        "китай": ("китай", "chinese", "china", "中国", "华语"),
        "истор": ("истор", "historical", "costume", "period drama", "古装"),
        "роман": ("роман", "romance", "love story", "爱情"),
        "трейлер": ("трейлер", "trailer", "promo", "预告"),
    }
    score = 0.0
    for token in _query_tokens(query):
        variants = next((values for prefix, values in aliases.items() if token.startswith(prefix)), (token,))
        if any(variant in haystack for variant in variants):
            score += 2.0
    return score


def _score_candidate(candidate: LicensedCandidate, query: str) -> float:
    token_score = _relevance_score(candidate, query)
    duration_score = min(candidate.duration, 900) / 300
    view_score = min(max(candidate.views, 0), 1_000_000) / 1_000_000
    source_bonus = 0.2 if candidate.source in {"Wikimedia Commons", "Internet Archive"} else 0.0
    return token_score + duration_score + view_score + source_bonus


def _quota(limit: int, variants: list[str]) -> int:
    return max(3, math.ceil(limit / max(len(variants), 1)))


def _search_entries(query: str, limit: int) -> list[dict]:
    entries: list[dict] = []
    variants = query_variants(query)
    for variant in variants:
        checkpoint()
        try:
            payload = json.loads(_run_yt_dlp(
                ["--flat-playlist", "--dump-single-json", f"ytsearch{_quota(limit, variants)}:{variant} Creative Commons"],
                timeout=120,
            ))
        except (RuntimeError, json.JSONDecodeError):
            continue
        entries.extend(item for item in payload.get("entries") or [] if isinstance(item, dict))
    unique: dict[str, dict] = {}
    for item in entries:
        key = str(item.get("id") or item.get("url") or "")
        if key:
            unique[key] = item
    return list(unique.values())[:limit]


def _youtube_url(entry: dict) -> str:
    url = str(entry.get("url") or "")
    if not url.startswith("http") and entry.get("id"):
        url = f"https://www.youtube.com/watch?v={entry['id']}"
    return url


def inspect_search_results(query: str, limit: int | None = None) -> list[dict]:
    """Вернуть результаты YouTube и статус разрешения; ничего не скачивать."""
    limit = limit or int(CONFIG["licensed_sources"]["youtube_search_results"])
    results: list[dict] = []
    for entry in _search_entries(query, limit):
        url = _youtube_url(entry)
        if not url:
            continue
        try:
            metadata = json.loads(_run_yt_dlp(["--skip-download", "--dump-json", url], timeout=90))
        except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
            continue
        candidate = _candidate_from_metadata(metadata)
        results.append({
            "video_id": str(metadata.get("id") or entry.get("id") or ""),
            "title": str(metadata.get("title") or entry.get("title") or ""),
            "channel": str(metadata.get("channel") or metadata.get("uploader") or ""),
            "url": url,
            "duration": float(metadata.get("duration") or 0),
            "views": int(metadata.get("view_count") or 0),
            "license": str(metadata.get("license") or "Не указана"),
            "download_allowed": candidate is not None,
            "permission_basis": candidate.permission_basis if candidate else None,
            "source": "YouTube",
        })
    return results


def _youtube_candidates(query: str, limit: int) -> list[LicensedCandidate]:
    entries = _search_entries(query, limit)

    def inspect(entry: dict) -> LicensedCandidate | None:
        url = _youtube_url(entry)
        if not url:
            return None
        metadata = json.loads(_run_yt_dlp(["--skip-download", "--dump-json", url], timeout=90))
        return _candidate_from_metadata(metadata)

    candidates: list[LicensedCandidate] = []
    with ThreadPoolExecutor(max_workers=min(4, max(len(entries), 1))) as pool:
        futures = [pool.submit(inspect, entry) for entry in entries]
        for future in as_completed(futures):
            checkpoint()
            try:
                candidate = future.result()
            except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
                continue
            if candidate and _relevance_score(candidate, query) > 0:
                candidates.append(candidate)
    return candidates


def _meta_value(metadata: dict, key: str) -> str:
    value = metadata.get(key) or {}
    return clean_text(value.get("value") if isinstance(value, dict) else value)


def _commons_candidates(query: str, limit: int) -> list[LicensedCandidate]:
    variants = query_variants(query)
    candidates: list[LicensedCandidate] = []
    seen: set[str] = set()
    max_bytes = int(CONFIG["licensed_sources"].get("max_download_mb", 2200)) * 1024 * 1024
    for variant in variants:
        checkpoint()
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "generator": "search", "gsrsearch": f"{variant} filetype:video",
                "gsrnamespace": 6, "gsrlimit": min(max(5, _quota(limit, variants)), 50),
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                "format": "json", "formatversion": 2,
            },
            headers={"User-Agent": USER_AGENT}, timeout=30,
        )
        response.raise_for_status()
        for page in (response.json().get("query") or {}).get("pages") or []:
            checkpoint()
            info = (page.get("imageinfo") or [{}])[0]
            download_url = str(info.get("url") or "")
            if not download_url or download_url in seen or not str(info.get("mime") or "").startswith("video/"):
                continue
            seen.add(download_url)
            metadata = info.get("extmetadata") or {}
            license_name = _meta_value(metadata, "LicenseShortName") or _meta_value(metadata, "UsageTerms")
            license_url = _meta_value(metadata, "LicenseUrl")
            basis = _open_license_basis(license_name, license_url)
            size = int(info.get("size") or 0)
            if not basis or (size and size > max_bytes):
                continue
            candidate = LicensedCandidate(
                video_id=f"commons-{page.get('pageid')}",
                title=_meta_value(metadata, "ObjectName") or clean_text(page.get("title")).removeprefix("File:"),
                channel=_meta_value(metadata, "Artist") or "Wikimedia Commons",
                channel_id="",
                url=str(info.get("descriptionurl") or ""),
                duration=float(info.get("duration") or 0),
                views=0,
                license=license_name,
                permission_basis=f"wikimedia-commons:{basis}",
                description=_meta_value(metadata, "ImageDescription")[:1200],
                source="Wikimedia Commons",
                download_url=download_url,
                size_bytes=size,
            )
            if _relevance_score(candidate, query) > 0:
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: _score_candidate(item, query), reverse=True)[:limit]


def _first(value: object) -> object:
    return value[0] if isinstance(value, list) and value else value


def _archive_download(metadata: dict, max_bytes: int) -> tuple[str, int]:
    choices: list[tuple[int, int, str]] = []
    priority = {".mp4": 3, ".webm": 2, ".mkv": 1, ".mov": 1, ".m4v": 1}
    for item in metadata.get("files") or []:
        name = str(item.get("name") or "")
        suffix = Path(urlparse(name).path).suffix.lower()
        if suffix not in priority or any(marker in name.lower() for marker in ("thumb", "sample")):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and size > max_bytes:
            continue
        choices.append((priority[suffix], size, name))
    if not choices:
        return "", 0
    _, size, name = max(choices, key=lambda item: (item[0], item[1]))
    identifier = str((metadata.get("metadata") or {}).get("identifier") or "")
    return f"https://archive.org/download/{quote(identifier)}/{quote(name)}", size


def _archive_candidates(query: str, limit: int) -> list[LicensedCandidate]:
    variants = query_variants(query)
    candidates: list[LicensedCandidate] = []
    seen: set[str] = set()
    max_bytes = int(CONFIG["licensed_sources"].get("max_download_mb", 2200)) * 1024 * 1024
    for variant in variants:
        checkpoint()
        response = requests.get(
            "https://archive.org/advancedsearch.php",
            params=[
                ("q", f"({variant}) AND mediatype:movies"), ("fl[]", "identifier"),
                ("fl[]", "title"), ("fl[]", "description"), ("fl[]", "creator"),
                ("fl[]", "downloads"), ("fl[]", "runtime"), ("fl[]", "licenseurl"),
                ("rows", max(5, _quota(limit, variants))), ("page", 1), ("output", "json"),
            ],
            headers={"User-Agent": USER_AGENT}, timeout=30,
        )
        response.raise_for_status()
        for row in (response.json().get("response") or {}).get("docs") or []:
            checkpoint()
            identifier = str(row.get("identifier") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            license_url = str(_first(row.get("licenseurl")) or "")
            basis = _open_license_basis("", license_url)
            title = clean_text(_first(row.get("title")))
            description = clean_text(_first(row.get("description")))[:1200]
            provisional = LicensedCandidate(
                video_id=f"archive-{identifier}", title=title, channel=clean_text(_first(row.get("creator"))) or "Internet Archive",
                channel_id="", url=f"https://archive.org/details/{identifier}",
                duration=float(parse_duration(_first(row.get("runtime"))) or 0), views=int(row.get("downloads") or 0),
                license=license_url, permission_basis=f"internet-archive:{basis}" if basis else "",
                description=description, source="Internet Archive",
            )
            if not basis or _relevance_score(provisional, query) <= 0:
                continue
            detail = requests.get(f"https://archive.org/metadata/{quote(identifier)}", headers={"User-Agent": USER_AGENT}, timeout=30)
            detail.raise_for_status()
            metadata = detail.json()
            download_url, size = _archive_download(metadata, max_bytes)
            if not download_url:
                continue
            item_meta = metadata.get("metadata") or {}
            candidates.append(LicensedCandidate(
                **{**asdict(provisional),
                   "duration": float(parse_duration(_first(item_meta.get("runtime"))) or provisional.duration),
                   "description": clean_text(_first(item_meta.get("description")))[:1200] or provisional.description,
                   "download_url": download_url, "size_bytes": size}
            ))
    return sorted(candidates, key=lambda item: _score_candidate(item, query), reverse=True)[:limit]


LICENSED_PROVIDERS = {
    "youtube": _youtube_candidates,
    "wikimedia_commons": _commons_candidates,
    "internet_archive": _archive_candidates,
}


def find_licensed_candidate(query: str, limit: int | None = None) -> LicensedCandidate:
    limit = limit or int(CONFIG["licensed_sources"]["youtube_search_results"])
    enabled = CONFIG["licensed_sources"].get("search_sources") or list(LICENSED_PROVIDERS)
    providers = [(name, LICENSED_PROVIDERS[name]) for name in enabled if name in LICENSED_PROVIDERS]
    print(f"[1/7] Ищу разрешённые видеоматериалы в {len(providers)} источниках: {query}")
    candidates: list[LicensedCandidate] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(len(providers), 3)) as pool:
        futures = {pool.submit(provider, query, limit): name for name, provider in providers}
        for future in as_completed(futures):
            checkpoint()
            name = futures[future]
            try:
                found = future.result()
                candidates.extend(found)
                print(f"  + {name}: разрешённых и релевантных — {len(found)}")
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
    for error in errors:
        print(f"  ! {error}")
    if not candidates:
        raise RuntimeError(
            "По запросу не найдено релевантного видео с подтверждённой лицензией CC BY/CC BY-SA/CC0 "
            "или public domain в YouTube, Wikimedia Commons и Internet Archive. "
            "Остальные площадки доступны только как ссылки в режиме радара."
        )
    return max(candidates, key=lambda item: _score_candidate(item, query))


def _safe_video_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)[:120] or "licensed-source"


def _download_direct(candidate: LicensedCandidate, destination_dir: Path) -> Path:
    suffix = Path(urlparse(candidate.download_url).path).suffix.lower()
    if suffix not in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ogv"}:
        suffix = ".mp4"
    path = destination_dir / f"{_safe_video_id(candidate.video_id)}{suffix}"
    max_bytes = int(CONFIG["licensed_sources"].get("max_download_mb", 2200)) * 1024 * 1024
    try:
        with requests.get(candidate.download_url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(30, 180)) as response:
            response.raise_for_status()
            expected = int(response.headers.get("Content-Length") or 0)
            if expected and expected > max_bytes:
                raise RuntimeError("Разрешённый файл слишком большой для автоматической загрузки")
            written = 0
            with path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    checkpoint()
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError("Загрузка остановлена: превышен лимит размера файла")
                    output.write(chunk)
            checkpoint()
    except JobCancelled:
        path.unlink(missing_ok=True)
        raise
    return path


def download_candidate(candidate: LicensedCandidate) -> tuple[Path, Path]:
    print(f"[2/7] Скачиваю разрешённый источник ({candidate.source}): {candidate.title}")
    destination_dir = INPUT_DIR / "licensed"
    destination_dir.mkdir(parents=True, exist_ok=True)
    if candidate.source == "YouTube":
        output_template = destination_dir / f"{_safe_video_id(candidate.video_id)}.%(ext)s"
        height = int(CONFIG["licensed_sources"].get("download_height", 1080))
        ffmpeg_dir = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
        try:
            _run_yt_dlp([
                "--no-playlist", "--ffmpeg-location", ffmpeg_dir,
                "-f", f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "--merge-output-format", "mp4", "-o", str(output_template), candidate.download_url or candidate.url,
            ], timeout=1800)
        except JobCancelled:
            for partial in destination_dir.glob(f"{_safe_video_id(candidate.video_id)}.*"):
                partial.unlink(missing_ok=True)
            raise
        matches = [
            path for path in destination_dir.glob(f"{_safe_video_id(candidate.video_id)}.*")
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        ]
        if not matches:
            raise RuntimeError("Видео прошло проверку, но файл после скачивания не найден")
        video_path = max(matches, key=lambda path: path.stat().st_mtime)
    else:
        video_path = _download_direct(candidate, destination_dir)
    metadata_path = destination_dir / f"{_safe_video_id(candidate.video_id)}.license.json"
    metadata_path.write_text(json.dumps(asdict(candidate), ensure_ascii=False, indent=2), encoding="utf-8")
    return video_path, metadata_path


def create_licensed_dorama_video(query: str, focus: str = "", limit: int | None = None) -> Path:
    candidate = find_licensed_candidate(query, limit)
    video_path, metadata_path = download_candidate(candidate)
    print(f"  Лицензия сохранена: {metadata_path.name}")
    print("[3/7] Передаю материал в пятиминутный монтаж...")
    return create_episode_recap(
        video_path,
        focus=focus or f"объяснить, почему материал связан с темой «{query}», без выдумывания фактов",
        source_info=asdict(candidate),
        progress_offset=2,
    )
