"""Русская озвучка: локальная Chatterbox V3 с безопасным откатом на Edge TTS."""
import asyncio
import json
import re
import shutil
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import edge_tts
import imageio_ffmpeg  # type: ignore[import-untyped]

from job_control import checkpoint, run_process
from settings import CONFIG, ROOT_DIR

_qa_model = None


def normalize_russian_text(text: str) -> str:
    replacements = {
        "YouTube": "Ютуб",
        "youtube": "Ютуб",
        "C-drama": "си-драма",
        "C-Drama": "си-драма",
        "2026 году": "две тысячи двадцать шестом году",
        "2026 года": "две тысячи двадцать шестого года",
        "2026": "две тысячи двадцать шестой",
    }
    for source, destination in replacements.items():
        text = text.replace(source, destination)
    return re.sub(r"\s+", " ", text).strip()


def split_for_tts(text: str, max_chars: int = 320) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?…])\s+", text) if item.strip()]
    units: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            units.append(sentence)
            continue
        words = sentence.split()
        current_words: list[str] = []
        for word in words:
            candidate = " ".join([*current_words, word])
            if current_words and len(candidate) > max_chars:
                units.append(" ".join(current_words))
                current_words = [word]
            else:
                current_words.append(word)
        if current_words:
            units.append(" ".join(current_words))
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def synthesize(text: str, output_path: Path, voice: str, rate: str = "+0%") -> Path:
    checkpoint()
    cfg = CONFIG["dorama"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_russian_text(text)
    pitch = cfg.get("pitch", "+0Hz")
    provider = cfg.get("tts_provider", "edge")
    if provider == "chatterbox":
        try:
            await asyncio.to_thread(_synthesize_chatterbox, normalized, output_path, cfg)
        except Exception as exc:
            if not cfg.get("tts_fallback_to_edge", True):
                raise
            print(f"  ! Chatterbox недоступна, использую Edge TTS: {exc}")
            await _synthesize_edge_chunked(normalized, output_path, voice, rate, pitch)
            provider = "edge-fallback"
    else:
        await _synthesize_edge_chunked(normalized, output_path, voice, rate, pitch)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Сервис озвучки не создал аудиофайл")
    if cfg.get("tts_qa_enabled", True):
        provider = await _quality_gate(normalized, output_path, voice, rate, pitch, provider, cfg)
        print(f"  TTS QA: выбран {provider}")
    return output_path


def _audio_duration(audio_path: Path) -> float:
    result = run_process(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(audio_path)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def _save_edge_chunk(text: str, path: Path, voice: str, rate: str, pitch: str) -> None:
    minimum_duration = max(0.7, len(text.split()) / 6.0)
    last_error: Exception | None = None
    for attempt in range(3):
        checkpoint()
        path.unlink(missing_ok=True)
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(str(path))
            duration = await asyncio.to_thread(_audio_duration, path)
            if path.is_file() and path.stat().st_size > 1000 and duration >= minimum_duration:
                return
            last_error = RuntimeError(
                f"Edge TTS вернула обрезанный фрагмент: {duration:.1f}с вместо минимум {minimum_duration:.1f}с"
            )
        except Exception as exc:  # noqa: BLE001 — единая retry-граница TTS-провайдера
            last_error = exc
        if attempt < 2:
            await asyncio.sleep(1.0 + attempt)
    raise RuntimeError(f"Edge TTS не смогла озвучить фрагмент после трёх попыток: {last_error}")


async def _synthesize_edge_chunked(
    text: str,
    output_path: Path,
    voice: str,
    rate: str,
    pitch: str,
    *,
    max_chars: int = 260,
) -> None:
    """Озвучивать небольшими частями, обнаруживать тихо обрезанные ответы и склеивать их."""
    chunks = split_for_tts(text, max_chars=max_chars)
    if not chunks:
        raise RuntimeError("Пустой текст для Edge TTS")
    parts_dir = output_path.parent / f".{output_path.stem}-edge-parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for index, chunk in enumerate(chunks):
            part = parts_dir / f"part-{index:04d}.mp3"
            await _save_edge_chunk(chunk, part, voice, rate, pitch)
            parts.append(part)
        checkpoint()
        concat_file = parts_dir / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{part.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for part in parts),
            encoding="utf-8",
        )
        temporary = output_path.with_name(f"{output_path.stem}.edge-merged{output_path.suffix}")
        codec = ["-c:a", "pcm_s16le"] if output_path.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-b:a", "128k"]
        result = run_process(
            [
                imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file), *codec, str(temporary),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"Не удалось объединить фрагменты Edge TTS: {(result.stderr or '')[-1200:]}")
        temporary.replace(output_path)
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)


def _synthesize_chatterbox(text: str, output_path: Path, cfg: dict[str, Any]) -> None:
    python_exe = ROOT_DIR / "tts-venv" / "Scripts" / "python.exe"
    worker = ROOT_DIR / "dorama" / "chatterbox_worker.py"
    if not python_exe.is_file():
        raise RuntimeError("не найдено отдельное окружение tts-venv")
    job_path = output_path.with_suffix(".tts-job.json")
    job = {
        "chunks": split_for_tts(text),
        "device": cfg.get("chatterbox_device", "cuda"),
        "voice_reference": cfg.get("chatterbox_voice_reference", ""),
        "cfg_weight": cfg.get("chatterbox_cfg_weight", 0.0),
        "exaggeration": cfg.get("chatterbox_exaggeration", 0.35),
        "temperature": cfg.get("chatterbox_temperature", 0.55),
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    result = run_process(
        [str(python_exe), str(worker), "--job", str(job_path), "--output", str(output_path)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    job_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or result.stdout[-2500:])


def _comparison_text(text: str) -> str:
    text = normalize_russian_text(text).lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def transcript_similarity(expected: str, actual: str) -> float:
    expected_words = _comparison_text(expected).split()
    actual_words = _comparison_text(actual).split()
    return SequenceMatcher(None, expected_words, actual_words).ratio()


def _transcribe_for_qa(audio_path: Path) -> str:
    global _qa_model
    if _qa_model is None:
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

        _qa_model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _qa_model.transcribe(str(audio_path), language="ru", vad_filter=True)
    text_parts = []
    for segment in segments:
        checkpoint()
        text_parts.append(segment.text.strip())
    return " ".join(text_parts).strip()


async def _quality_gate(
    expected: str,
    output_path: Path,
    voice: str,
    rate: str,
    pitch: str,
    provider: str,
    cfg: dict[str, Any],
) -> str:
    transcript = await asyncio.to_thread(_transcribe_for_qa, output_path)
    score = transcript_similarity(expected, transcript)
    candidates = [(score, provider, output_path, transcript)]

    if provider == "chatterbox" and score < cfg["tts_qa_min_similarity"]:
        edge_path = output_path.with_name(f"{output_path.stem}.edge{output_path.suffix}")
        await _synthesize_edge_chunked(expected, edge_path, voice, rate, pitch)
        edge_transcript = await asyncio.to_thread(_transcribe_for_qa, edge_path)
        edge_score = transcript_similarity(expected, edge_transcript)
        candidates.append((edge_score, "edge-qa-fallback", edge_path, edge_transcript))

    if provider.startswith("edge") and score < cfg["tts_qa_min_similarity"]:
        retry_path = output_path.with_name(f"{output_path.stem}.edge-retry{output_path.suffix}")
        await _synthesize_edge_chunked(expected, retry_path, voice, rate, pitch, max_chars=160)
        retry_transcript = await asyncio.to_thread(_transcribe_for_qa, retry_path)
        retry_score = transcript_similarity(expected, retry_transcript)
        candidates.append((retry_score, "edge-qa-retry", retry_path, retry_transcript))

    best_score, best_provider, best_path, best_transcript = max(candidates, key=lambda item: item[0])
    if best_path != output_path:
        shutil.move(str(best_path), str(output_path))
    for _, _, path, _ in candidates:
        if path != output_path and path.exists():
            path.unlink()

    report = output_path.with_suffix(".qa.txt")
    report.write_text(
        f"provider={best_provider}\nscore={best_score:.4f}\nthreshold={cfg['tts_qa_min_similarity']:.4f}"
        f"\n\nEXPECTED:\n{expected}\n\nTRANSCRIPT:\n{best_transcript}\n",
        encoding="utf-8",
    )
    if best_score < cfg["tts_qa_min_similarity"]:
        raise RuntimeError(
            f"Озвучка не прошла проверку точности: {best_score:.1%} < "
            f"{cfg['tts_qa_min_similarity']:.1%}. Отчёт: {report}"
        )
    return best_provider
