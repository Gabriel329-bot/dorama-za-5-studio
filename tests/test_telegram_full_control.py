from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from telegram import InlineKeyboardMarkup

from telegram_bot import bot
from telegram_bot import keyboards as kb
from webapp.app import get_pipeline_settings, uploaded_video_metadata

SETTINGS = {
    "whisper_model": "medium",
    "whisper_device": "cuda",
    "ollama_model": "qwen2.5:7b",
    "search_sources": ["youtube", "wikimedia_commons"],
    "voice": "ru-RU-DmitryNeural",
    "rate": "+12%",
    "pitch": "-2Hz",
    "target_duration_seconds": 300,
    "scene_count": 9,
    "original_audio_volume": 0.16,
    "target_script_words": 680,
    "require_review": True,
}


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.chat_id = 100
        self.message_id = 200
        self.rendered = ""
        self.reply_markup = None

    async def edit_text(self, text: str, **kwargs) -> FakeMessage:
        self.rendered = text
        self.reply_markup = kwargs.get("reply_markup")
        return self

    async def reply_text(self, text: str, **kwargs) -> FakeMessage:
        self.rendered = text
        self.reply_markup = kwargs.get("reply_markup")
        return self


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.answer_text = None

    async def answer(self, **kwargs) -> None:
        self.answer_text = kwargs.get("text")


class FakeUpdate:
    effective_user = SimpleNamespace(id=42)

    def __init__(self, *, callback: str | None = None, text: str = "") -> None:
        self.callback_query = FakeCallback(callback) if callback else None
        self.effective_message = FakeMessage(text)


class FakeBot:
    async def edit_message_text(self, text: str, **_kwargs) -> None:
        self.last_text = text


class FakeContext:
    def __init__(self, user_data: dict | None = None) -> None:
        self.user_data = user_data or {}
        self.bot = FakeBot()


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_every_telegram_callback_fits_platform_limit() -> None:
    clip = {"id": 123, "status": "pending", "file_exists": True}
    job = {"id": "abcdef123456", "status": "running", "title": "Процесс"}
    markups = [
        kb.main_menu(),
        kb.create_menu(),
        kb.create_options("licensed", 40),
        kb.translation_interval(7200),
        kb.queue_filters("pending"),
        kb.queue_list([clip], filter_status="pending"),
        kb.clip_actions(clip),
        kb.publish_privacy(123),
        kb.jobs_list([job]),
        kb.job_actions("abcdef123456", running=True),
        kb.settings_main(),
        kb.whisper_settings("medium", "cuda"),
        kb.voice_settings("ru-RU-DmitryNeural", "+12%", "-2Hz"),
        kb.pipeline_settings(SETTINGS),
        kb.source_settings(["youtube"]),
        kb.youtube_settings(True, "private"),
    ]
    callbacks = [callback for markup in markups for callback in _callbacks(markup)]
    assert callbacks
    assert max(len(callback.encode("utf-8")) for callback in callbacks) <= 64


def test_translation_interval_parser_supports_clock_and_enforces_five_minutes() -> None:
    assert bot._parse_interval("04:12-09:12", 1200) == (252.0, 552.0)
    assert bot._parse_interval("12.5-22.5", 30) == (12.5, 22.5)
    with pytest.raises(ValueError, match="5 минут"):
        bot._parse_interval("00:00-05:01", 1000)
    with pytest.raises(ValueError, match="длительность"):
        bot._parse_interval("09:00-10:00", 550)


def test_pipeline_setting_is_saved_through_shared_api() -> None:
    with (
        patch.object(bot.api, "pipeline_settings", return_value=dict(SETTINGS)),
        patch.object(bot.api, "save_pipeline_settings", return_value={"ok": True}) as save,
    ):
        _, section = asyncio.run(bot._save_pipeline_value("model", "small"))
    payload = save.call_args.args[0]
    assert payload["whisper_model"] == "small"
    assert section == "whisper"


def test_last_search_source_cannot_be_disabled() -> None:
    settings = {**SETTINGS, "search_sources": ["youtube"]}
    with (
        patch.object(bot.api, "pipeline_settings", return_value=settings),
        patch.object(bot.api, "save_pipeline_settings") as save,
        pytest.raises(ValueError, match="последний источник"),
    ):
        asyncio.run(bot._save_pipeline_value("source", "youtube"))
    save.assert_not_called()


def test_delete_callback_uses_delete_api_and_refreshes_shared_queue() -> None:
    update = FakeUpdate(callback="clip:delete:7")
    context = FakeContext({"queue_filter": "all"})
    with (
        patch.object(bot, "ALLOWED_USER", 42),
        patch.object(bot.api, "delete_clip", return_value={"ok": True}) as delete,
        patch.object(bot.api, "clips", return_value=[]),
    ):
        asyncio.run(bot.on_callback(update, context))
    delete.assert_called_once_with(7)
    assert "Очередь публикаций" in update.callback_query.message.rendered


def test_public_publish_callback_does_not_fall_back_to_private() -> None:
    update = FakeUpdate(callback="clip:privacypublic:7")
    context = FakeContext()
    with (
        patch.object(bot, "ALLOWED_USER", 42),
        patch.object(bot.api, "publish_youtube", return_value={"id": "job-7", "title": "YouTube"}) as publish,
    ):
        asyncio.run(bot.on_callback(update, context))
    publish.assert_called_once_with(7, "public")
    assert "job-7" in update.callback_query.message.rendered


def test_clear_process_history_callback_keeps_action_explicit() -> None:
    update = FakeUpdate(callback="jobs:clear:yes")
    with (
        patch.object(bot, "ALLOWED_USER", 42),
        patch.object(bot.api, "clear_job_history", return_value={"cleared": 4}) as clear,
    ):
        asyncio.run(bot.on_callback(update, FakeContext()))
    clear.assert_called_once_with()
    assert "4" in update.callback_query.message.rendered


def test_caption_text_is_written_via_api_and_refreshed() -> None:
    update = FakeUpdate(text="Новое описание\n#дорамы")
    context = FakeContext({"awaiting": "edit_caption", "edit_clip_id": 9})
    clip = {"id": 9, "status": "pending", "file_exists": True}
    with (
        patch.object(bot, "ALLOWED_USER", 42),
        patch.object(bot.api, "edit_caption", return_value={"ok": True}) as edit,
        patch.object(bot.api, "clip", return_value=clip),
    ):
        asyncio.run(bot.on_text(update, context))
    edit.assert_called_once_with(9, "Новое описание\n#дорамы")
    assert "awaiting" not in context.user_data


def test_existing_input_metadata_uses_same_duration_probe_as_web_upload() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        input_dir = Path(temporary)
        source = input_dir / "episode.mp4"
        source.write_bytes(b"video")
        with (
            patch("webapp.app.INPUT_DIR", input_dir),
            patch("webapp.app.probe_video_duration", return_value=901.25),
        ):
            result = uploaded_video_metadata(source.name)
    assert result["filename"] == source.name
    assert result["duration_seconds"] == 901.25
    assert result["editor_required"] is True


def test_legacy_config_without_audio_volume_remains_readable() -> None:
    config = {
        "whisper": {"model": "medium", "device": "cuda"},
        "highlight": {"model": "qwen2.5:7b"},
        "dorama": {
            "search_sources": ["youtube"],
            "voice": "ru-RU-DmitryNeural",
            "rate": "+12%",
            "pitch": "-2Hz",
            "target_duration_seconds": 300,
            "target_script_words": 680,
            "require_review": True,
        },
        "episode": {"scene_count": 9},
    }
    with patch("webapp.app.CONFIG", config):
        settings = get_pipeline_settings()
    assert settings["original_audio_volume"] == 0.16
