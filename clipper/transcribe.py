"""Транскрибация видео в текст с таймкодами по словам через faster-whisper."""
from dataclasses import dataclass

from faster_whisper import WhisperModel

from settings import CONFIG
from job_control import checkpoint

_model_cache: dict[str, WhisperModel] = {}


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    full_text: str
    words: list[Word]


def _get_model() -> WhisperModel:
    checkpoint()
    model_size = CONFIG["whisper"]["model"]
    device = CONFIG["whisper"]["device"]
    if model_size not in _model_cache:
        compute_type = "int8" if device == "cpu" else "float16"
        _model_cache[model_size] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _model_cache[model_size]


def transcribe(video_path: str, language: str | None = None) -> Transcript:
    """Возвращает полный текст и список слов с таймкодами (в секундах от начала видео)."""
    checkpoint()
    model = _get_model()
    selected_language = CONFIG["whisper"]["language"] if language is None else language
    if selected_language == "auto":
        selected_language = None

    segments, _info = model.transcribe(
        video_path,
        language=selected_language,
        word_timestamps=True,
        vad_filter=True,
    )

    words: list[Word] = []
    text_parts: list[str] = []
    for segment in segments:
        checkpoint()
        text_parts.append(segment.text.strip())
        if segment.words:
            for w in segment.words:
                words.append(Word(text=w.word.strip(), start=w.start, end=w.end))

    if not words:
        raise RuntimeError(
            "Whisper не распознал речь в видео — проверь, что в файле есть звук и голос."
        )

    return Transcript(full_text=" ".join(text_parts), words=words)
