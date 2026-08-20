import asyncio
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
from dorama.source_pipeline import _parse_json_response
from dorama.speech import _quality_gate, split_for_tts
from job_control import JobCancelled, cancellation_scope, checkpoint, submit_cancellable
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
