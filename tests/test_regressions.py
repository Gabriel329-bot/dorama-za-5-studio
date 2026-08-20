import asyncio
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import requests
import yaml
from pydantic import ValidationError
from telegram.ext import MessageHandler

from config_store import update_yaml_text
from dorama.licensed_sources import LicensedCandidate, _download_direct
from dorama.render import _fit_audio
from dorama.source_pipeline import (
    _NARRATION_SCHEMA,
    TimelineItem,
    _align_translation,
    _ask_ollama,
    _grow_narration,
    _load_translation_cache,
    _parse_json_response,
    _require_russian_narration,
    _russian_word_count,
    _translate_timeline,
)
from dorama.speech import _quality_gate, split_for_tts
from job_control import JobCancelled, cancellation_scope, checkpoint, submit_cancellable
from storage import db
from telegram_bot import bot, keyboards
from webapp.app import PipelineSettingsRequest, save_pipeline_settings


class ConfigStoreRegressionTests(unittest.TestCase):
    def test_block_list_is_replaced_without_corrupting_yaml(self):
        original = """publishing:
  youtube:
    enabled: false  # keep
    post_times:
    - '12:00'
    privacy_status: private
dorama:
  voice: ru-RU-DmitryNeural
"""
        updated = update_yaml_text(
            original,
            {
                ("publishing", "youtube", "enabled"): True,
                ("publishing", "youtube", "post_times"): ["13:00", "19:00"],
            },
        )
        parsed = yaml.safe_load(updated)
        self.assertEqual(["13:00", "19:00"], parsed["publishing"]["youtube"]["post_times"])
        self.assertTrue(parsed["publishing"]["youtube"]["enabled"])
        self.assertIn("# keep", updated)
        self.assertNotIn("- '12:00'", updated)

    def test_pipeline_settings_reject_unknown_source_and_voice(self):
        values = {
            "whisper_model": "medium",
            "whisper_device": "cuda",
            "ollama_model": "qwen2.5:7b",
            "search_sources": ["not-a-provider"],
            "voice": "bad-Neural",
            "rate": "+12%",
            "pitch": "-2Hz",
            "target_duration_seconds": 300,
            "scene_count": 9,
            "original_audio_volume": 0.16,
            "target_script_words": 680,
            "require_review": True,
        }
        with self.assertRaises(ValidationError):
            PipelineSettingsRequest(**values)

    def test_pipeline_settings_update_episode_duration_and_words(self):
        original = """whisper:
  model: medium
  device: cuda
highlight:
  model: qwen2.5:7b
dorama:
  search_sources: [youtube]
  voice: ru-RU-DmitryNeural
  rate: '+12%'
  pitch: '-2Hz'
  target_duration_seconds: 300
  target_script_words: 680
  require_review: true  # keep
episode:
  target_duration_seconds: 300
  target_narration_words: 680
  scene_count: 9
  original_audio_volume: 0.16
"""
        payload = PipelineSettingsRequest(
            whisper_model="small",
            whisper_device="cpu",
            ollama_model="qwen2.5:3b",
            search_sources=["youtube", "wikimedia_commons"],
            voice="ru-RU-SvetlanaNeural",
            rate="+8%",
            pitch="+0Hz",
            target_duration_seconds=420,
            scene_count=12,
            original_audio_volume=0.2,
            target_script_words=900,
            require_review=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(original, encoding="utf-8")
            memory_config = yaml.safe_load(original)
            with (
                patch("webapp.app.CONFIG_PATH", path),
                patch("webapp.app.CONFIG", memory_config),
            ):
                save_pipeline_settings(payload)
            updated_text = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(updated_text)
        self.assertEqual(420, parsed["episode"]["target_duration_seconds"])
        self.assertEqual(900, parsed["episode"]["target_narration_words"])
        self.assertEqual(900, parsed["dorama"]["target_script_words"])
        self.assertIn("# keep", updated_text)


class TelegramRegressionTests(unittest.TestCase):
    def test_confirmation_callback_does_not_embed_user_text(self):
        markup = keyboards.confirm_create("dorama")
        callback = markup.inline_keyboard[0][0].callback_data
        self.assertEqual("run:dorama", callback)
        self.assertLessEqual(len(callback.encode("utf-8")), 64)

    def test_text_handler_is_registered(self):
        application = bot._build_application("123:ABC")
        handlers = [handler for group in application.handlers.values() for handler in group]
        self.assertTrue(any(isinstance(handler, MessageHandler) for handler in handlers))

    def test_youtube_action_does_not_reject_clip(self):
        class User:
            id = 42

        class Message:
            def __init__(self):
                self.text = ""

            async def edit_text(self, text, **_kwargs):
                self.text = text

        class Callback:
            data = "clip:yt:7"

            def __init__(self):
                self.message = Message()

            async def answer(self, **_kwargs):
                return None

        class Update:
            effective_user = User()

            def __init__(self):
                self.callback_query = Callback()

        class Context:
            def __init__(self) -> None:
                self.user_data: dict[str, object] = {}

        update = Update()
        with (
            patch.object(bot, "ALLOWED_USER", 42),
            patch.object(bot.api, "publish_youtube", return_value={"id": "job123"}) as publish,
            patch.object(bot.api, "reject_clip") as reject,
        ):
            asyncio.run(bot.on_callback(update, Context()))
        publish.assert_called_once_with(7, "private")
        reject.assert_not_called()
        self.assertIn("job123", update.callback_query.message.text)


class NarrationAndAudioRegressionTests(unittest.TestCase):
    def test_json_parser_accepts_fence_and_surrounding_text(self):
        self.assertEqual({"ok": 1}, _parse_json_response("```json\n{\"ok\":1}\n```"))
        self.assertEqual({"ok": 2}, _parse_json_response("Ответ модели: {\"ok\":2} после JSON"))

    def test_json_parser_recovers_common_ollama_wrappers(self):
        self.assertEqual(
            {"narration": "текст"},
            _parse_json_response('"{\\"narration\\":\\"текст\\"}"'),
        )
        self.assertEqual(
            {"narration": "текст"},
            _parse_json_response('[{"narration":"текст"}]'),
        )
        self.assertEqual(
            {"narration": "текст"},
            _parse_json_response('{"response":{"narration":"текст"}}'),
        )

    @patch("dorama.source_pipeline.ollama_generate")
    def test_ollama_request_uses_schema_and_output_budget(self, generate):
        generate.return_value = '{"narration":"готово"}'

        result = _ask_ollama("prompt", schema=_NARRATION_SCHEMA)

        self.assertEqual({"narration": "готово"}, result)
        payload = generate.call_args.args[1]
        self.assertEqual(_NARRATION_SCHEMA, payload["format"])
        self.assertEqual(4096, payload["options"]["num_predict"])
        self.assertIn("только на естественном русском", payload["system"])

    def test_russian_narration_validation_rejects_chinese_response(self):
        self.assertEqual(0, _russian_word_count("这是一个中文故事。"))
        with self.assertRaisesRegex(ValueError, "не на русском"):
            _require_russian_narration({"narration": "这是一个中文故事。"})

        russian = " ".join(["Это"] * 12)
        _require_russian_narration({"narration": russian})

    @patch("dorama.source_pipeline._ask_ollama")
    def test_timeline_translation_preserves_program_owned_timestamps(self, ask):
        source = [
            TimelineItem(1.5, 10.0, "第一段"),
            TimelineItem(10.0, 20.25, "第二段"),
        ]
        ask.return_value = {
            "items": [
                {"text": " ".join(["Первый"] * 6)},
                {"text": " ".join(["Второй"] * 6)},
            ]
        }

        with tempfile.TemporaryDirectory() as tmp, patch(
            "dorama.source_pipeline._translation_cache_path",
            return_value=Path(tmp) / "translation.json",
        ):
            translated = _translate_timeline(source)

        self.assertEqual((1.5, 10.0), (translated[0].start, translated[0].end))
        self.assertEqual((10.0, 20.25), (translated[1].start, translated[1].end))
        self.assertEqual(1, ask.call_count)

    def test_translation_is_redistributed_when_model_changes_item_count(self):
        source = [
            TimelineItem(0.0, 10.0, "короткий"),
            TimelineItem(10.0, 30.0, "намного более длинный исходный сегмент"),
        ]

        aligned = _align_translation(
            source,
            ["один два три четыре пять шесть семь восемь девять десять"],
        )

        self.assertEqual((0.0, 10.0), (aligned[0].start, aligned[0].end))
        self.assertEqual((10.0, 30.0), (aligned[1].start, aligned[1].end))
        self.assertEqual(10, len(" ".join(item.text for item in aligned).split()))
        self.assertGreater(len(aligned[1].text.split()), len(aligned[0].text.split()))

    def test_translation_cache_preserves_timestamps(self):
        source = [TimelineItem(2.0, 8.0, "中文")]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "translation.json"
            cache.write_text(
                '{"texts":["Это сохранённый естественный русский перевод исходного временного сегмента, который программа использует при повторном запуске"]}',
                encoding="utf-8",
            )

            translated = _load_translation_cache(cache, source)

        self.assertIsNotNone(translated)
        assert translated is not None
        self.assertEqual((2.0, 8.0), (translated[0].start, translated[0].end))

    @patch("dorama.source_pipeline._extend_narration")
    @patch("dorama.source_pipeline._expand_narration")
    def test_narration_growth_never_accepts_shorter_rewrite(self, expand, extend):
        original = " ".join(["Начало"] * 374)
        expand.return_value = " ".join(["Короче"] * 120)
        extend.side_effect = [
            " ".join(["Продолжение"] * 130),
            " ".join(["Финал"] * 110),
        ]

        result = _grow_narration(original, [], "", 680)

        self.assertEqual(614, _russian_word_count(result))
        self.assertTrue(result.startswith(original))
        self.assertEqual(2, extend.call_count)

    @patch("dorama.source_pipeline._extend_narration")
    @patch("dorama.source_pipeline._expand_narration")
    def test_narration_growth_accepts_many_small_additions(self, expand, extend):
        original = " ".join(["Начало"] * 122)
        expand.return_value = " ".join(["Короче"] * 80)
        extend.side_effect = [" ".join([f"Блок{index}"] * 44) for index in range(12)]

        result = _grow_narration(original, [], "", 680)

        self.assertEqual(606, _russian_word_count(result))
        self.assertEqual(11, extend.call_count)
    def test_very_short_audio_is_not_stretched_to_five_minutes(self):
        with (
            patch("dorama.render._audio_duration", return_value=112.0),
            self.assertRaisesRegex(RuntimeError, "ухудшит качество"),
        ):
            _fit_audio(Path("voice.mp3"), 300.0, Path("fitted.m4a"))

    def test_tts_split_never_leaves_oversized_sentence(self):
        text = " ".join(["длинноеслово"] * 80) + "."
        chunks = split_for_tts(text, max_chars=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_edge_qa_retries_truncated_audio(self):
        async def fake_synthesize(_text, path, *_args, **_kwargs):
            path.write_bytes(b"retry-audio")

        cfg = {"tts_qa_min_similarity": 0.88}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.mp3"
            output.write_bytes(b"short")
            with (
                patch("dorama.speech._transcribe_for_qa", side_effect=["обрыв", "точный текст"]),
                patch("dorama.speech._synthesize_edge_chunked", new=fake_synthesize),
            ):
                provider = asyncio.run(
                    _quality_gate("точный текст", output, "ru-RU-DmitryNeural", "+12%", "-2Hz", "edge", cfg)
                )
            self.assertEqual("edge-qa-retry", provider)
            self.assertEqual(b"retry-audio", output.read_bytes())


class DatabaseMigrationRegressionTests(unittest.TestCase):
    def test_legacy_clips_table_adds_origin_job_before_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE clips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    source_video TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    platform TEXT,
                    created_at TEXT NOT NULL,
                    posted_at TEXT
                )
                """
            )
            connection.commit()
            connection.close()

            with patch.object(db, "DB_PATH", database):
                db.init_db()

            connection = sqlite3.connect(database)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(clips)")
            }
            indexes = {
                str(row[1]) for row in connection.execute("PRAGMA index_list(clips)")
            }
            connection.close()

        self.assertIn("origin_job_id", columns)
        self.assertIn("idx_clips_origin_job", indexes)


class DownloadRegressionTests(unittest.TestCase):
    class Response:
        def __init__(self, status_code, chunks=()):
            self.status_code = status_code
            self.headers = {"Content-Length": str(sum(len(chunk) for chunk in chunks))}
            self.chunks = chunks
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            self.closed = True

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def iter_content(self, chunk_size):
            return iter(self.chunks)

    def test_429_retry_closes_response_and_downloads(self):
        limited = self.Response(429)
        video_bytes = b"\x00\x00\x00\x18ftypisom" + b"video"
        success = self.Response(200, [video_bytes])
        candidate = LicensedCandidate(
            video_id="safe",
            title="Test",
            channel="Author",
            channel_id="channel",
            url="https://example.test/page",
            duration=60,
            views=1,
            license="CC0",
            permission_basis="public-domain-or-cc0",
            source="Wikimedia Commons",
            download_url="https://upload.wikimedia.org/video.mp4",
            size_bytes=len(video_bytes),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("dorama.licensed_sources.requests.get", side_effect=[limited, success]) as get,
                patch("dorama.licensed_sources.cancellable_wait") as wait,
            ):
                path = _download_direct(candidate, Path(tmp))
            self.assertEqual(video_bytes, path.read_bytes())
        self.assertEqual(2, get.call_count)
        wait.assert_called_once_with(5)
        self.assertTrue(limited.closed)
        self.assertTrue(success.closed)


class CancellationRegressionTests(unittest.TestCase):
    def test_thread_contexts_keep_cancellation_addressable(self):
        first_event = Event()
        second_event = Event()

        def check_after_delay():
            time.sleep(0.05)
            checkpoint()
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as pool:
            with cancellation_scope(first_event):
                first = submit_cancellable(pool, check_after_delay)
            with cancellation_scope(second_event):
                second = submit_cancellable(pool, check_after_delay)
            first_event.set()
            with self.assertRaises(JobCancelled):
                first.result(timeout=1)
            self.assertEqual("ok", second.result(timeout=1))


if __name__ == "__main__":
    unittest.main()
