"""Рендер вертикального клипа с горящими субтитрами через ffmpeg."""
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

from clipper.highlight import Highlight
from clipper.transcribe import Word
from job_control import run_process
from settings import CONFIG
from video_accel import selected_encoder_options

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
WORDS_PER_CAPTION_CHUNK = 4


def _ass_escape_path(path: Path) -> str:
    """ffmpeg -vf subtitles=... требует экранирования ':' и '\\' в пути на Windows."""
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace(":", "\\:")
    return p


def _chunk_words(words: list[Word]) -> list[list[Word]]:
    return [words[i : i + WORDS_PER_CAPTION_CHUNK] for i in range(0, len(words), WORDS_PER_CAPTION_CHUNK)]


def _fmt_ass_time(seconds: float) -> str:
    seconds = max(seconds, 0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _build_ass(words: list[Word], start: float, end: float, ass_path: Path) -> None:
    cfg = CONFIG["render"]
    in_range = [w for w in words if w.end > start and w.start < end]
    rel_words = [
        Word(text=w.text, start=max(0.0, w.start - start), end=min(end - start, w.end - start))
        for w in in_range
    ]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {cfg['output_width']}
PlayResY: {cfg['output_height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg['font']},{cfg['font_size']},{cfg['base_color']},{cfg['base_color']},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    base_color = cfg["base_color"]
    hl_color = cfg["highlight_color"]

    for chunk in _chunk_words(rel_words):
        if not chunk:
            continue
        for i, active in enumerate(chunk):
            if active.end <= active.start:
                continue
            parts = []
            for j, w in enumerate(chunk):
                color = hl_color if j == i else base_color
                parts.append(f"{{\\c{color}}}{w.text}{{\\c{base_color}}}")
            text = " ".join(parts)
            line = (
                f"Dialogue: 0,{_fmt_ass_time(active.start)},{_fmt_ass_time(active.end)},"
                f"Default,,0,0,0,,{text}"
            )
            lines.append(line)

    ass_path.write_text("\n".join(lines), encoding="utf-8")


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
