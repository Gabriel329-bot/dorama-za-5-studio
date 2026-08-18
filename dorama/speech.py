"""Русская озвучка: локальная Chatterbox V3 с безопасным откатом на Edge TTS."""
import asyncio
from difflib import SequenceMatcher
import json
import re
from pathlib import Path
import shutil
import subprocess

import edge_tts

from settings import CONFIG, ROOT_DIR
from job_control import checkpoint, run_process

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
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
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
            communicate = edge_tts.Communicate(text=normalized, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(str(output_path))
            provider = "edge-fallback"
    else:
        communicate = edge_tts.Communicate(text=normalized, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Сервис озвучки не создал аудиофайл")
    if cfg.get("tts_qa_enabled", True):
        provider = await _quality_gate(normalized, output_path, voice, rate, pitch, provider, cfg)
        print(f"  TTS QA: выбран {provider}")
    return output_path


def _synthesize_chatterbox(text: str, output_path: Path, cfg: dict) -> None:
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
        from faster_whisper import WhisperModel

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
    cfg: dict,
) -> str:
    transcript = await asyncio.to_thread(_transcribe_for_qa, output_path)
    score = transcript_similarity(expected, transcript)
    candidates = [(score, provider, output_path, transcript)]

    if provider == "chatterbox" and score < cfg["tts_qa_min_similarity"]:
        edge_path = output_path.with_name(f"{output_path.stem}.edge{output_path.suffix}")
        communicate = edge_tts.Communicate(text=expected, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(str(edge_path))
        edge_transcript = await asyncio.to_thread(_transcribe_for_qa, edge_path)
        edge_score = transcript_similarity(expected, edge_transcript)
        candidates.append((edge_score, "edge-qa-fallback", edge_path, edge_transcript))

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
