"""Рендер вертикального клипа с горящими субтитрами через ffmpeg."""
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

from clipper.highlight import Highlight
from clipper.transcribe import Word
from job_control import run_process
from media_pipeline.resources import render_slot
from media_pipeline.subtitles import build_cues, write_ass
from settings import CONFIG
from video_accel import selected_encoder_options

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()


def _ass_escape_path(path: Path) -> str:
    """ffmpeg -vf subtitles=... требует экранирования ':' и '\\' в пути на Windows."""
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace(":", "\\:")
    return p


def _fmt_ass_time(seconds: float) -> str:
    seconds = max(seconds, 0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _build_ass(words: list[Word], start: float, end: float, ass_path: Path) -> None:
    in_range = [w for w in words if w.end > start and w.start < end]
    rel_words = [
        Word(text=w.text, start=max(0.0, w.start - start), end=min(end - start, w.end - start))
        for w in in_range
    ]
    duration = end - start
    cues = build_cues(rel_words, duration)
    cfg = CONFIG["render"]
    write_ass(
        cues,
        ass_path,
        width=int(cfg["output_width"]),
        height=int(cfg["output_height"]),
    )


def render_clip(source_video: str, highlight: Highlight, words: list[Word], output_path: Path) -> Path:
    cfg = CONFIG["render"]
    ass_path = output_path.with_suffix(".ass")
    _build_ass(words, highlight.start, highlight.end, ass_path)

    duration = highlight.end - highlight.start
    vf = (
        f"scale=w={cfg['output_width']}:h={cfg['output_height']}:force_original_aspect_ratio=increase,"
        f"crop={cfg['output_width']}:{cfg['output_height']},"
        f"subtitles='{_ass_escape_path(ass_path)}'"
    )

    encoder, encoder_args = selected_encoder_options()
    print(f"  FFmpeg encoder: {encoder}")
    cmd = [
        FFMPEG_EXE,
        "-y",
        "-ss", str(highlight.start),
        "-i", source_video,
        "-t", str(duration),
        "-vf", vf,
        *encoder_args,
        "-c:a", "aac",
        "-b:a", "160k",
        str(output_path),
    ]

    try:
        with render_slot(encoder):
            result = run_process(cmd, capture_output=True, text=True)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        ass_path.unlink(missing_ok=True)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg упал при рендере {output_path.name}:\n{result.stderr[-3000:]}")
    return output_path
