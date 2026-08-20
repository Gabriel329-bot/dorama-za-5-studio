from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clipper.transcribe import Transcript, Word, transcribe
from media_pipeline.audio import (
    _demucs_command,
    ducking_mix_filter,
    measure_loudness,
    render_ducked_mix,
    stream_duration,
)
from media_pipeline.subtitles import (
    SubtitlePolicy,
    SubtitleValidationError,
    build_cues,
    validate_cues,
    wrap_caption,
    write_ass,
)
from video_accel import _probe_encoder

POLICY = SubtitlePolicy(
    min_cps=12,
    max_cps=17,
    max_cpl=42,
    max_lines=2,
    min_duration=0.9,
    max_duration=6,
    gap=1 / 30,
    frame_rate=30,
    scene_snap=0.16,
    max_word_gap=0.75,
)


def test_caption_wrap_never_exceeds_cpl() -> None:
    lines = wrap_caption(
        "Она даже не догадывалась кем на самом деле был этот незнакомец",
        max_cpl=37,
        max_lines=2,
    )
    assert len(lines) == 2
    assert all(len(line) <= 37 for line in lines)


def test_word_timestamps_produce_readable_non_overlapping_cues() -> None:
    words = [
        Word(text, index * 0.42, index * 0.42 + 0.35)
        for index, text in enumerate(
            [
                "Она",
                "не",
                "знала",
                "что",
                "эта",
                "встреча",
                "навсегда",
                "изменит",
                "её",
                "судьбу",
            ]
        )
    ]
    cues = build_cues(words, duration=5.0, policy=POLICY)
    validate_cues(cues, 5.0, POLICY)
    assert cues
    assert all(cue.duration >= 0.899 for cue in cues)
    assert all(cue.cps <= 17.05 for cue in cues)


def test_scene_cut_is_a_hard_subtitle_boundary() -> None:
    words = [
        Word("первая", 0.0, 0.6),
        Word("сцена.", 0.6, 1.3),
        Word("другая", 2.0, 2.6),
        Word("сцена.", 2.6, 3.3),
    ]
    cues = build_cues(words, 4.0, scene_cuts=[1.8], policy=POLICY)
    assert all(not (cue.start < 1.8 < cue.end) for cue in cues)


def test_impossible_reading_speed_is_rejected() -> None:
    words = [
        Word("оченьдлинныйтекст", 0.0, 0.1),
        Word("безпаузы", 0.1, 0.2),
        Word("ещёбыстрее", 0.2, 0.3),
        Word("финал", 0.3, 0.4),
    ]
    with pytest.raises(SubtitleValidationError, match="CPS"):
        build_cues(words, duration=0.6, policy=POLICY)


def test_ass_writer_escapes_override_sequences() -> None:
    cues = build_cues(
        [Word(r"текст{опасный}\\код", 0.0, 1.5)],
        duration=2.0,
        policy=POLICY,
    )
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "captions.ass"
        write_ass(cues, output)
        content = output.read_text(encoding="utf-8")
    assert "{опасный}" not in content
    assert r"\\код" not in content


def test_loudness_measurement_parses_ffmpeg_json() -> None:
    report = {
        "input_i": "-22.1",
        "input_tp": "-3.2",
        "input_lra": "8.1",
        "input_thresh": "-33.0",
        "target_offset": "0.2",
    }
    process = SimpleNamespace(returncode=0, stderr=json.dumps(report), stdout="")
    with patch("media_pipeline.audio.run_process", return_value=process):
        measured = measure_loudness(
            Path("bed.wav"),
            target_i=-24,
            target_tp=-1.5,
            target_lra=11,
        )
    assert measured is not None
    assert measured.input_i == -22.1
    assert measured.target_offset == 0.2


def test_stream_duration_reads_last_packet_pts_without_decoding() -> None:
    process = SimpleNamespace(
        returncode=0,
        stderr="frame=9000 time=00:04:59.97 bitrate=N/A",
        stdout="",
    )
    with patch("media_pipeline.audio.run_process", return_value=process) as runner:
        duration = stream_duration(Path("final.mp4"), "0:v:0", 300.0)
    assert duration == 299.97
    command = runner.call_args.args[0]
    assert command[command.index("-c") + 1] == "copy"


def test_mix_filter_uses_sidechain_drift_control_and_peak_limiter() -> None:
    value = ducking_mix_filter(1, 2, duration=300)
    assert "sidechaincompress=" in value
    assert "aresample=48000:async=1:first_pts=0" in value
    assert "alimiter=limit=" in value
    assert "level=false" in value
    assert "atrim=duration=300.000000" in value


def test_ducked_mix_gets_a_separate_two_pass_final_normalization() -> None:
    expected = Path("work/mix-final.wav")
    with (
        patch("media_pipeline.audio._run_ffmpeg") as ffmpeg,
        patch(
            "media_pipeline.audio.normalize_loudness",
            return_value=expected,
        ) as normalize,
    ):
        actual = render_ducked_mix(
            Path("bed.wav"),
            Path("voice.wav"),
            Path("work"),
            duration=300,
        )
    assert actual == expected
    ffmpeg.assert_called_once()
    assert normalize.call_args.kwargs["target_i"] == -14.0
    assert normalize.call_args.kwargs["target_tp"] == -1.5
    assert normalize.call_args.kwargs["limiter_db"] == -3.6


def test_demucs_command_is_argument_list_not_shell_text() -> None:
    command = _demucs_command(
        Path("python.exe"),
        Path("mix with spaces.wav"),
        Path("output"),
        "cuda",
    )
    assert isinstance(command, list)
    assert command[-1] == "mix with spaces.wav"
    assert command[command.index("--two-stems") + 1] == "vocals"


def test_cuda_whisper_inference_uses_shared_accelerator_slot() -> None:
    transcript = Transcript("тест", [Word("тест", 0.0, 0.5)])
    with (
        patch.dict("clipper.transcribe.CONFIG", {"whisper": {"device": "cuda", "language": "ru", "cuda_fallback": False}}),
        patch("clipper.transcribe._load_cached_transcript", return_value=None),
        patch("clipper.transcribe._get_model", return_value=object()),
        patch("clipper.transcribe._run_transcription", return_value=transcript),
        patch("clipper.transcribe._save_cached_transcript"),
        patch("clipper.transcribe.accelerator_slot") as slot,
    ):
        actual = transcribe("voice.wav", language="ru")
    assert actual == transcript
    slot.assert_called_once_with("whisper")


def test_failed_whisper_transcription_releases_cached_models() -> None:
    with (
        patch.dict("clipper.transcribe.CONFIG", {"whisper": {"device": "cpu", "language": "ru", "cuda_fallback": False}}),
        patch("clipper.transcribe._load_cached_transcript", return_value=None),
        patch("clipper.transcribe._get_model", return_value=object()),
        patch("clipper.transcribe._run_transcription", side_effect=RuntimeError("bad audio")),
        patch("clipper.transcribe.release_models") as release,
        pytest.raises(RuntimeError, match="bad audio"),
    ):
        transcribe("broken.wav", language="ru")
    release.assert_called_once_with()


def test_nvenc_probe_uses_supported_frame_dimensions() -> None:
    process = SimpleNamespace(returncode=0, stdout="", stderr="")
    _probe_encoder.cache_clear()
    try:
        with (
            patch("video_accel._listed_encoders", return_value={"h264_nvenc"}),
            patch("video_accel.run_process", return_value=process) as runner,
        ):
            assert _probe_encoder("h264_nvenc") is True
        command = runner.call_args.args[0]
        assert "color=c=black:s=256x256:r=1:d=0.1" in command
    finally:
        _probe_encoder.cache_clear()
