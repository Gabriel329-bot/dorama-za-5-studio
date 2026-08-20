import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

from clipper.highlight import Highlight, _fit_duration, _overlaps_existing, _validate
from clipper.render import _build_ass, _fmt_ass_time
from clipper.transcribe import (
    Transcript,
    Word,
    _compute_type,
    _load_cached_transcript,
    _save_cached_transcript,
    _transcript_cache_key,
)
from dorama.discover import _parse_search, is_relevant, parse_duration, query_variants
from dorama.licensed_sources import (
    LicensedCandidate,
    _cached_download,
    _candidate_from_metadata,
    _open_license_basis,
    _permission_basis,
    _relevance_score,
    _score_candidate,
)
from dorama.render import _sentences
from dorama.script import (
    _ensure_narration_length,
    _grounded_narration,
    _normalize_hashtags,
)
from dorama.source_pipeline import (
    SourceScene,
    _build_sequence,
    _make_timeline,
    _validate_scenes,
)
from dorama.speech import normalize_russian_text, split_for_tts, transcript_similarity
from job_control import (
    JobCancelled,
    cancellation_scope,
    checkpoint,
    ollama_generate,
    run_process,
)
from publisher.youtube import build_metadata
from video_accel import encoder_options


class HighlightValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "min_segment_seconds": 20,
            "max_segment_seconds": 59,
            "max_segments_per_video": 5,
        }

    def test_short_segment_is_expanded(self):
        start, end = _fit_duration(30, 35, video_end=120, min_len=20, max_len=59)
        self.assertEqual(20, end - start)
        self.assertGreaterEqual(start, 0)

    def test_long_segment_is_trimmed(self):
        self.assertEqual((10, 69), _fit_duration(10, 100, 120, 20, 59))

    def test_invalid_and_overlapping_items_are_removed(self):
        items = [
            {"start": 10, "end": 15, "caption": "первый"},
            {"start": 12, "end": 18, "caption": "дубль"},
            {"start": "нет", "end": 40},
            {"start": 70, "end": 100},
        ]
        result = _validate(items, video_end=120, cfg=self.cfg)
        self.assertEqual(2, len(result))
        self.assertEqual("первый", result[0].caption)
        self.assertEqual("Смотри до конца", result[1].caption)

    def test_overlap_ratio(self):
        chosen = [Highlight(10, 30, "a")]
        self.assertTrue(_overlaps_existing(15, 35, chosen))
        self.assertFalse(_overlaps_existing(29, 49, chosen))


class SubtitleTests(unittest.TestCase):
    def test_ass_time(self):
        self.assertEqual("1:01:01.25", _fmt_ass_time(3661.25))

    def test_ass_contains_each_word_and_dialogues(self):
        words = [Word("один", 5.0, 5.5), Word("два", 5.5, 6.0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.ass"
            _build_ass(words, start=5.0, end=7.0, ass_path=path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("один", content)
        self.assertIn("два", content)
        # Одна читаемая реплика не должна мигать на каждом слове.
        self.assertEqual(1, content.count("Dialogue:"))


class DoramaTests(unittest.TestCase):
    def test_search_results_are_ranked_by_views(self):
        payload = {"entries": [
            {"id": "one", "title": "Первый", "view_count": 10, "channel": "A"},
            {"id": "two", "title": "Второй", "view_count": 1000, "channel": "B"},
        ]}
        result = _parse_search(payload)
        self.assertEqual("Второй", result[0].title)
        self.assertEqual("https://www.youtube.com/watch?v=two", result[0].url)

    def test_multilingual_query_variants_and_duration_parser(self):
        variants = query_variants("исторические китайские дорамы")
        self.assertTrue(any("Chinese drama" in item for item in variants))
        self.assertTrue(any("中国电视剧" in item for item in variants))
        self.assertEqual(3723, parse_duration("1:02:03"))

    def test_relevance_filter_removes_music_named_drama(self):
        query = "исторические китайские дорамы"
        self.assertTrue(is_relevant("Chinese historical costume drama episode", query))
        self.assertFalse(is_relevant("aespa Drama rolling lyrics K-Pop", query))

    def test_hashtags_are_clean_and_unique(self):
        result = _normalize_hashtags(["#Дорамы", " c-drama! ", "#Новинки"], ["#дорамы"])
        self.assertEqual(["#дорамы", "#cdrama", "#Новинки"], result)

    def test_narration_is_split_into_subtitles(self):
        self.assertEqual(["Раз.", "Два!", "Три?"], _sentences("Раз. Два! Три?"))

    def test_short_script_gets_safe_editorial_expansion(self):
        result = _ensure_narration_length("Короткий текст.")
        self.assertGreaterEqual(len(result.split()), 45)
        self.assertIn("поисковой выдаче", result)

    def test_grounded_narration_uses_only_observed_counts(self):
        trends = _parse_search({"entries": [
            {"id": "a", "title": "Лучшие исторические дорамы", "view_count": 100},
            {"id": "b", "title": "New historical romance dramas", "view_count": 50},
        ]})
        result = _grounded_narration(trends, "тест")
        self.assertIn("2 открытых", result)
        self.assertIn("в 2 из 2", result)
        self.assertIn("не рейтинг", result)

    def test_tts_normalizes_year_and_brand(self):
        result = normalize_russian_text("YouTube в 2026 году")
        self.assertEqual("Ютуб в две тысячи двадцать шестом году", result)

    def test_tts_splits_long_text_on_sentences(self):
        result = split_for_tts("Первая фраза. Вторая длинная фраза. Третья.", max_chars=30)
        self.assertEqual(2, len(result))

    def test_tts_similarity_ignores_case_and_punctuation(self):
        score = transcript_similarity("Дорамы — это интересно!", "дорамы это интересно")
        self.assertEqual(1.0, score)

    def test_tts_similarity_penalizes_changed_words(self):
        score = transcript_similarity("Проверим новинки и жанры", "Потеряем картинки и жанр")
        self.assertLess(score, 0.5)

    def test_episode_timeline_preserves_time_buckets(self):
        words = [
            Word("первая", 0, 1),
            Word("сцена", 10, 11),
            Word("вторая", 80, 81),
        ]
        timeline = _make_timeline(words, bucket_seconds=60)
        self.assertEqual(2, len(timeline))
        self.assertEqual("первая сцена", timeline[0].text)
        self.assertEqual(80, timeline[1].start)

    def test_episode_scene_validation_clamps_and_removes_bad_items(self):
        scenes = _validate_scenes(
            [
                {"start": 5, "end": 40, "reason": "завязка"},
                {"start": 20, "end": 60, "reason": "перекрытие"},
                {"start": 100, "end": 190, "reason": "слишком длинная"},
                {"start": "нет", "end": 20},
                {"start": 180, "end": 205, "reason": "финальная сцена"},
            ],
            duration=200,
            desired_count=5,
        )
        self.assertEqual(3, len(scenes))
        self.assertEqual(55, scenes[1].duration)
        self.assertEqual(200, scenes[2].end)

    def test_episode_sequence_fills_exact_target(self):
        sequence = _build_sequence(
            [SourceScene(0, 12), SourceScene(30, 41)],
            target_duration=50,
        )
        self.assertAlmostEqual(50, sum(scene.duration for scene in sequence))
        self.assertEqual(5, len(sequence))

    def test_creative_commons_source_is_allowed(self):
        metadata = {
            "id": "video1",
            "title": "Chinese drama clip",
            "channel": "Author",
            "channel_id": "UC1",
            "webpage_url": "https://www.youtube.com/watch?v=video1",
            "license": "Creative Commons Attribution license (reuse allowed)",
            "availability": "public",
            "duration": 120,
        }
        candidate = _candidate_from_metadata(metadata)
        self.assertIsNotNone(candidate)
        self.assertEqual("youtube-creative-commons", candidate.permission_basis)

    def test_unlicensed_source_is_not_allowed(self):
        self.assertIsNone(_permission_basis({"license": "Standard YouTube License", "channel_id": "UCX"}))

    def test_open_license_rejects_noncommercial_and_no_derivatives(self):
        self.assertEqual("creative-commons-by", _open_license_basis("CC BY 4.0"))
        self.assertEqual("public-domain-or-cc0", _open_license_basis("Public domain"))
        self.assertIsNone(_open_license_basis("CC BY-NC 4.0"))
        self.assertIsNone(_open_license_basis("", "https://creativecommons.org/licenses/by-nd/4.0/"))

    def test_licensed_search_prefers_relevant_result(self):
        common = {
            "video_id": "a",
            "channel": "Author",
            "channel_id": "UC1",
            "url": "https://example.com/a",
            "duration": 180,
            "views": 100,
            "license": "Creative Commons Attribution license (reuse allowed)",
            "permission_basis": "youtube-creative-commons",
        }
        relevant = LicensedCandidate(title="Китайская историческая дорама", description="романтика", **common)
        unrelated = LicensedCandidate(title="Action stock footage", description="cars", **common)
        self.assertGreater(_score_candidate(relevant, "историческая дорама"), _score_candidate(unrelated, "историческая дорама"))
        self.assertGreater(_relevance_score(relevant, "китайская дорама"), 0)
        self.assertEqual(0, _relevance_score(unrelated, "китайская дорама"))

    def test_whisper_auto_compute_type_uses_gpu_float16_and_cpu_int8(self):
        self.assertEqual("float16", _compute_type("cuda"))
        self.assertEqual("int8", _compute_type("cpu"))

    def test_transcript_cache_round_trip_and_file_change_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "episode.mp4"
            video.write_bytes(b"first-version")
            first_key = _transcript_cache_key(video, "zh")
            transcript = Transcript("первая сцена", [Word("первая", 0.0, 0.5), Word("сцена", 0.5, 1.0)])
            with patch("clipper.transcribe.CACHE_DIR", root / "cache"):
                _save_cached_transcript(video, "zh", transcript)
                cached = _load_cached_transcript(video, "zh")
            self.assertEqual(transcript, cached)
            video.write_bytes(b"second-version-is-longer")
            self.assertNotEqual(first_key, _transcript_cache_key(video, "zh"))

    def test_encoder_options_cover_hardware_and_software_fallback(self):
        self.assertIn("h264_nvenc", encoder_options("h264_nvenc"))
        self.assertIn("h264_qsv", encoder_options("h264_qsv"))
        software = encoder_options("libx264", still_image=True)
        self.assertIn("libx264", software)
        self.assertIn("stillimage", software)

    def test_verified_download_is_reused_from_cache(self):
        candidate = LicensedCandidate(
            video_id="cached-source",
            title="Chinese drama",
            channel="Author",
            channel_id="",
            url="https://example.com/video",
            duration=300,
            views=0,
            license="CC BY",
            permission_basis="creative-commons-by",
            source="Wikimedia Commons",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "cached-source.mp4"
            video.write_bytes(b"video")
            metadata = root / "cached-source.license.json"
            metadata.write_text(
                '{"video_id":"cached-source","permission_basis":"creative-commons-by"}',
                encoding="utf-8",
            )
            self.assertEqual(video, _cached_download(candidate, root, metadata))


class YouTubeMetadataTests(unittest.TestCase):
    def test_hashtags_become_youtube_tags_and_sources_are_appended(self):
        clip = {
            "file_path": "video.mp4",
            "caption": "Дорама-радар\n\n#дорамы #CDrama #дорамы",
            "source_video": "dorama-trends:https://example.com/one|https://example.com/two",
        }
        result = build_metadata(clip, "private")
        self.assertEqual("Дорама-радар", result["snippet"]["title"])
        self.assertEqual(["дорамы", "CDrama"], result["snippet"]["tags"])
        self.assertIn("https://example.com/one", result["snippet"]["description"])
        self.assertEqual("private", result["status"]["privacyStatus"])
        self.assertTrue(result["status"]["containsSyntheticMedia"])


class JobCancellationTests(unittest.TestCase):
    class _SlowOllamaResponse:
        def __init__(self, delay: float = 0.15):
            self.delay = delay
            self.closed = False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            time.sleep(self.delay)
            yield '{"response":"готово","done":true}'.encode()

        def close(self):
            self.closed = True

    class _FakeSession:
        def __init__(self, response):
            self.response = response
            self.post_kwargs = None

        def post(self, _url, **kwargs):
            self.post_kwargs = kwargs
            return self.response

        def close(self):
            return None

    def test_ollama_waits_for_slow_first_token_and_keeps_model_warm(self):
        response = self._SlowOllamaResponse()
        session = self._FakeSession(response)
        with patch("job_control.requests.Session", return_value=session):
            result = ollama_generate("http://ollama/api/generate", {"model": "test"}, timeout=2)
        self.assertEqual("готово", result)
        self.assertEqual("15m", session.post_kwargs["json"]["keep_alive"])
        self.assertEqual((10, 60), session.post_kwargs["timeout"])

    def test_ollama_can_be_cancelled_before_first_token(self):
        event = Event()
        response = self._SlowOllamaResponse(delay=2)
        session = self._FakeSession(response)
        timer = Timer(0.1, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with (
                patch("job_control.requests.Session", return_value=session),
                self.assertRaises(JobCancelled),
                cancellation_scope(event),
            ):
                ollama_generate("http://ollama/api/generate", {"model": "test"}, timeout=5)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 1)

    def test_cancellation_stops_only_selected_queued_job(self):
        from webapp.app import _submit_job, cancel_job, jobs, jobs_lock

        def waits_for_cancel():
            while True:
                checkpoint()
                time.sleep(0.02)

        first = _submit_job("test", "Первая", waits_for_cancel)
        deadline = time.time() + 2
        while time.time() < deadline:
            with jobs_lock:
                if jobs[first["id"]]["status"] == "running":
                    break
            time.sleep(0.02)
        second = _submit_job("test", "Вторая", lambda: "не должна запуститься")
        result = cancel_job(second["id"])
        self.assertEqual("cancelled", result["status"])
        with jobs_lock:
            self.assertEqual("running", jobs[first["id"]]["status"])
            self.assertNotIn(second["id"], jobs)
        cancel_job(first["id"])
        deadline = time.time() + 2
        while time.time() < deadline:
            with jobs_lock:
                if first["id"] not in jobs:
                    break
            time.sleep(0.02)
        with jobs_lock:
            self.assertNotIn(first["id"], jobs)

    def test_cancel_terminates_child_process(self):
        event = Event()
        timer = Timer(0.2, event.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(JobCancelled), cancellation_scope(event):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=35)
        timer.cancel()
        self.assertLess(time.monotonic() - started, 4)


if __name__ == "__main__":
    unittest.main()
