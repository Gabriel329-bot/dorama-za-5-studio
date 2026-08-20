"""Word-level субтитры с ограничениями CPS/CPL и привязкой к кадрам."""
from __future__ import annotations

import math
import os
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import imageio_ffmpeg  # type: ignore[import-untyped]

from job_control import run_process
from settings import CONFIG

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
_PUNCTUATION_END = re.compile(r"[.!?…:;][\"'»)]*$")
_SCENE_TIME = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


class WordLike(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...


@dataclass(frozen=True)
class SubtitlePolicy:
    min_cps: float = 12.0
    max_cps: float = 17.0
    max_cpl: int = 42
    max_lines: int = 2
    min_duration: float = 0.9
    max_duration: float = 6.0
    gap: float = 1 / 30
    frame_rate: float = 30.0
    scene_snap: float = 0.16
    max_word_gap: float = 0.75

    @classmethod
    def from_config(cls) -> SubtitlePolicy:
        cfg = CONFIG.get("subtitles", {})
        return cls(
            min_cps=float(cfg.get("min_cps", 12.0)),
            max_cps=float(cfg.get("max_cps", 17.0)),
            max_cpl=int(cfg.get("max_cpl", 42)),
            max_lines=int(cfg.get("max_lines", 2)),
            min_duration=float(cfg.get("min_duration", 0.9)),
            max_duration=float(cfg.get("max_duration", 6.0)),
            gap=float(cfg.get("gap_seconds", 1 / 30)),
            frame_rate=float(cfg.get("frame_rate", 30.0)),
            scene_snap=float(cfg.get("scene_snap_seconds", 0.16)),
            max_word_gap=float(cfg.get("max_word_gap", 0.75)),
        )


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def cps(self) -> float:
        characters = len(self.text.replace(" ", ""))
        return characters / self.duration if self.duration > 0 else math.inf


class SubtitleValidationError(ValueError):
    """Субтитры нарушают читаемость или временную монотонность."""


def _frame_floor(value: float, frame_rate: float) -> float:
    return math.floor(max(value, 0.0) * frame_rate + 1e-9) / frame_rate


def _frame_ceil(value: float, frame_rate: float) -> float:
    return math.ceil(max(value, 0.0) * frame_rate - 1e-9) / frame_rate


def _join_words(words: Sequence[WordLike]) -> str:
    text = " ".join(word.text.strip() for word in words if word.text.strip())
    return re.sub(r"\s+([,.;:!?…»)])", r"\1", text).strip()


def wrap_caption(text: str, max_cpl: int = 42, max_lines: int = 2) -> tuple[str, ...]:
    tokens: list[str] = []
    for token in re.sub(r"\s+", " ", text).strip().split(" "):
        if len(token) <= max_cpl:
            tokens.append(token)
        else:
            tokens.extend(
                token[index : index + max_cpl]
                for index in range(0, len(token), max_cpl)
            )
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}".strip()
        if current and len(candidate) > max_cpl:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        raise SubtitleValidationError(
            f"Текст не помещается в {max_lines} строки по {max_cpl} символов"
        )
    return tuple(lines)


def _valid_words(words: Iterable[WordLike], duration: float) -> list[WordLike]:
    result: list[WordLike] = []
    last_start = 0.0
    for word in words:
        text = re.sub(r"\s+", " ", str(word.text)).strip()
        start = float(word.start)
        end = float(word.end)
        if not text or not all(math.isfinite(value) for value in (start, end)):
            continue
        start = max(0.0, min(start, duration))
        end = max(start, min(end, duration))
        if end <= start or start + 0.25 < last_start:
            continue
        last_start = start
        result.append(_Word(text, start, end))
    return result


@dataclass(frozen=True)
class _Word:
    text: str
    start: float
    end: float


def _crosses_cut(start: float, end: float, scene_cuts: Sequence[float]) -> bool:
    index = bisect_right(scene_cuts, start)
    return index < len(scene_cuts) and scene_cuts[index] <= end


def _group_words(
    words: Sequence[WordLike],
    policy: SubtitlePolicy,
    scene_cuts: Sequence[float],
) -> list[list[WordLike]]:
    groups: list[list[WordLike]] = []
    current: list[WordLike] = []
    max_chars = policy.max_cpl * policy.max_lines

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for word in words:
        if current and (
            word.start - current[-1].end > policy.max_word_gap
            or _crosses_cut(current[0].start, word.start, scene_cuts)
        ):
            flush()
        candidate = [*current, word]
        candidate_text = _join_words(candidate)
        candidate_duration = max(word.end - candidate[0].start, 1e-6)
        if current and (
            len(candidate_text) > max_chars
            or candidate_duration > policy.max_duration
        ):
            flush()
            candidate = [word]
            candidate_text = word.text
            candidate_duration = max(word.end - word.start, 1e-6)
        current = candidate
        candidate_cps = len(candidate_text.replace(" ", "")) / candidate_duration
        readable = (
            candidate_duration >= policy.min_duration
            and policy.min_cps <= candidate_cps <= policy.max_cps
        )
        if readable and _PUNCTUATION_END.search(candidate_text) is not None:
            flush()
    flush()
    return groups


def _nearest_cut(value: float, cuts: Sequence[float], distance: float) -> float | None:
    index = bisect_left(cuts, value)
    nearby = [
        cuts[candidate]
        for candidate in (index - 1, index)
        if 0 <= candidate < len(cuts) and abs(cuts[candidate] - value) <= distance
    ]
    return min(nearby, key=lambda cut: abs(cut - value)) if nearby else None


def build_cues(
    words: Iterable[WordLike],
    duration: float,
    *,
    scene_cuts: Iterable[float] = (),
    policy: SubtitlePolicy | None = None,
) -> list[SubtitleCue]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Длительность субтитров должна быть положительной")
    selected_policy = policy or SubtitlePolicy.from_config()
    cuts = sorted(
        {
            _frame_floor(float(cut), selected_policy.frame_rate)
            for cut in scene_cuts
            if math.isfinite(float(cut)) and 0 < float(cut) < duration
        }
    )
    normalized = _valid_words(words, duration)
    groups = _group_words(normalized, selected_policy, cuts)
    cues: list[SubtitleCue] = []
    for index, group in enumerate(groups):
        lines = wrap_caption(
            _join_words(group),
            selected_policy.max_cpl,
            selected_policy.max_lines,
        )
        raw_start = group[0].start
        raw_end = group[-1].end
        snapped_start = _nearest_cut(raw_start, cuts, selected_policy.scene_snap)
        start = _frame_floor(
            snapped_start if snapped_start is not None else raw_start,
            selected_policy.frame_rate,
        )
        if cues:
            start = max(start, cues[-1].end + selected_policy.gap)
        characters = len("".join(lines).replace(" ", ""))
        required = max(
            selected_policy.min_duration,
            characters / selected_policy.max_cps,
        )
        desired_end = max(raw_end, start + required)
        snapped_end = _nearest_cut(desired_end, cuts, selected_policy.scene_snap)
        if snapped_end is not None and snapped_end > start:
            desired_end = snapped_end
        next_cut_index = bisect_right(cuts, start)
        if next_cut_index < len(cuts) and cuts[next_cut_index] < desired_end:
            desired_end = cuts[next_cut_index]
        if index + 1 < len(groups):
            next_start = _frame_floor(
                groups[index + 1][0].start,
                selected_policy.frame_rate,
            )
            desired_end = min(desired_end, next_start - selected_policy.gap)
        end = min(
            _frame_ceil(desired_end, selected_policy.frame_rate),
            duration,
            start + selected_policy.max_duration,
        )
        needed_duration = max(
            selected_policy.min_duration,
            characters / selected_policy.max_cps,
        )
        if end - start + 1e-6 < needed_duration:
            previous_boundary = (
                cues[-1].end + selected_policy.gap if cues else 0.0
            )
            scene_index = bisect_right(cuts, start)
            scene_boundary = cuts[scene_index - 1] if scene_index else 0.0
            earliest_start = max(previous_boundary, scene_boundary)
            start = max(
                earliest_start,
                _frame_floor(
                    end - needed_duration,
                    selected_policy.frame_rate,
                ),
            )
        cues.append(SubtitleCue(start, end, lines))
    repaired = _repair_cue_timing(
        cues,
        cuts,
        duration,
        selected_policy,
    )
    validate_cues(repaired, duration, selected_policy)
    return repaired


def _repair_cue_timing(
    cues: Sequence[SubtitleCue],
    cuts: Sequence[float],
    duration: float,
    policy: SubtitlePolicy,
) -> list[SubtitleCue]:
    """Расписать реплики назад от hard cuts, не превышая max CPS."""
    grouped: dict[int, list[tuple[int, SubtitleCue]]] = defaultdict(list)
    for index, cue in enumerate(cues):
        scene_index = bisect_right(cuts, cue.start + 1e-6)
        grouped[scene_index].append((index, cue))

    repaired: list[SubtitleCue | None] = [None] * len(cues)
    boundaries = [0.0, *cuts, duration]
    for scene_index, indexed_cues in grouped.items():
        left_boundary = boundaries[scene_index]
        right_boundary = boundaries[scene_index + 1]
        next_start = right_boundary
        for index, cue in reversed(indexed_cues):
            characters = len(cue.text.replace(" ", ""))
            required = max(policy.min_duration, characters / policy.max_cps)
            latest_end = next_start - (
                policy.gap if next_start < right_boundary else 0.0
            )
            end = min(cue.end, latest_end)
            start = min(cue.start, end - required)
            start = max(
                left_boundary,
                _frame_floor(start, policy.frame_rate),
            )
            end = min(
                right_boundary,
                _frame_ceil(end, policy.frame_rate),
                start + policy.max_duration,
            )
            repaired[index] = SubtitleCue(start, end, cue.lines)
            next_start = start
    return [cue for cue in repaired if cue is not None]


def validate_cues(
    cues: Sequence[SubtitleCue],
    duration: float,
    policy: SubtitlePolicy | None = None,
) -> None:
    selected_policy = policy or SubtitlePolicy.from_config()
    previous_end = 0.0
    errors: list[str] = []
    for index, cue in enumerate(cues, start=1):
        if not all(math.isfinite(value) for value in (cue.start, cue.end)):
            errors.append(f"#{index}: нечисловой таймкод")
            continue
        if cue.start < previous_end - 1e-6:
            errors.append(f"#{index}: пересечение с предыдущей репликой")
        if cue.duration + 1e-6 < selected_policy.min_duration:
            errors.append(f"#{index}: показ {cue.duration:.2f}с < {selected_policy.min_duration:.2f}с")
        if cue.duration - 1e-6 > selected_policy.max_duration:
            errors.append(f"#{index}: показ {cue.duration:.2f}с > {selected_policy.max_duration:.2f}с")
        if cue.cps > selected_policy.max_cps + 0.05:
            errors.append(f"#{index}: CPS {cue.cps:.1f} > {selected_policy.max_cps:.1f}")
        if len(cue.lines) > selected_policy.max_lines:
            errors.append(f"#{index}: больше {selected_policy.max_lines} строк")
        if any(len(line) > selected_policy.max_cpl for line in cue.lines):
            errors.append(f"#{index}: строка длиннее {selected_policy.max_cpl} символов")
        if cue.end > duration + 1e-6:
            errors.append(f"#{index}: конец за длительностью видео")
        previous_end = cue.end
    if errors:
        raise SubtitleValidationError("; ".join(errors[:8]))


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "／")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours:d}:{minutes:02d}:{remainder:05.2f}"


def write_ass(
    cues: Sequence[SubtitleCue],
    output_path: Path,
    *,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    cfg = CONFIG.get("render", {})
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.get('font', 'Arial')},{cfg.get('font_size', 58)},&H00FFFFFF,&H00FFFFFF,&H00140A14,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,70,70,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        text = r"\N".join(_ass_escape(line) for line in cue.lines)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},"
            f"Default,,0,0,0,,{text}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def detect_scene_cuts(
    source_path: Path,
    *,
    start_seconds: float,
    duration: float,
) -> list[float]:
    cfg = CONFIG.get("subtitles", {})
    threshold = float(cfg.get("scene_threshold", 0.32))
    filter_value = (
        "scale=320:-2,select='gt(scene,"
        f"{threshold:.3f})',showinfo"
    )
    result = run_process(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{max(start_seconds, 0.0):.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(source_path),
            "-an",
            "-vf",
            filter_value,
            "-vsync",
            "vfr",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        timeout=max(60.0, duration * 1.5),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Не удалось определить смены сцен:\n" + (result.stderr or "")[-2000:]
        )
    return sorted(
        {
            float(match.group(1))
            for match in _SCENE_TIME.finditer(result.stderr or "")
            if 0 < float(match.group(1)) < duration
        }
    )
