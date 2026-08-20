"""Полный Telegram-интерфейс к локальной студии «Дорама за 5».

Telegram и web используют один FastAPI, одну SQLite-очередь и один JobManager.
Бот не содержит отдельного состояния публикаций и потому всегда синхронизирован
с панелью после следующего чтения API.
"""
# ruff: noqa: BLE001,S110 — обработчики являются внешней UI-границей Telegram
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Lock, Thread
from typing import Any, ParamSpec, TypeVar, cast

from telegram import CallbackQuery, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from telegram_bot import api_client as api
from telegram_bot import keyboards as kb
from telegram_bot.media import prepare_telegram_preview

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
log = logging.getLogger("dorama-bot")

try:
    ALLOWED_USER = int(os.environ.get("TELEGRAM_USER_ID", "0"))
except ValueError:
    ALLOWED_USER = 0
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

STATUS_EMOJI = {
    "running": "🔄",
    "queued": "⏳",
    "paused": "⏸",
    "attention": "⚠️",
    "succeeded": "✅",
    "failed": "❌",
    "cancelled": "🚫",
    "cancelling": "⏹",
}
ACTIVE_STATUSES = frozenset({"running", "queued", "paused", "cancelling"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "attention"})
_WORKFLOW_KEYS = (
    "awaiting",
    "create_type",
    "create_query",
    "create_limit",
    "episode",
    "edit_clip_id",
)

_bot_thread: Thread | None = None
_bot_stop_event: ThreadEvent | None = None
_bot_lock = Lock()
P = ParamSpec("P")
T = TypeVar("T")


def _progress_bar(pct: int, length: int = 10) -> str:
    safe_pct = max(0, min(100, int(pct)))
    filled = round(safe_pct / 100 * length)
    return "█" * filled + "░" * (length - filled) + f" {safe_pct}%"


def _auth(update: Update) -> bool:
    return bool(
        ALLOWED_USER
        and update.effective_user
        and update.effective_user.id == ALLOWED_USER
    )


def _h(value: object) -> str:
    return html.escape(str(value), quote=False)


def _limit_text(value: str, limit: int = 3900) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fmt_duration(seconds: float) -> str:
    total = max(0, round(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    if context.user_data is None:
        raise RuntimeError("Telegram-сессия недоступна")
    return cast(dict[str, Any], context.user_data)


def _clear_workflow(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    for key in _WORKFLOW_KEYS:
        state.pop(key, None)


def _set_control(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is not None and message_id is not None:
        state["control_chat_id"] = chat_id
        state["control_message_id"] = message_id


async def _answer(cb: CallbackQuery, text: str | None = None) -> None:
    try:
        await cb.answer(text=text, show_alert=False)
    except Exception:
        pass


async def _edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(
            _limit_text(text),
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _edit_control(
    fallback: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    state = _state(context)
    chat_id = state.get("control_chat_id")
    message_id = state.get("control_message_id")
    if chat_id is not None and message_id is not None:
        try:
            await context.bot.edit_message_text(
                _limit_text(text),
                chat_id=int(chat_id),
                message_id=int(message_id),
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    sent = await fallback.reply_text(
        _limit_text(text),
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    _set_control(sent, context)


async def _call_api(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Не блокировать event loop синхронным localhost HTTP/FFmpeg I/O."""
    return await asyncio.to_thread(function, *args, **kwargs)


def _clip_status(status: object) -> str:
    return {
        "pending": "На проверке",
        "publishing": "Публикуется",
        "posted": "Опубликовано",
        "rejected": "Отклонено",
        "deleting": "Удаляется",
    }.get(str(status), str(status))


def _job_text(job: api.JsonDict) -> str:
    emoji = STATUS_EMOJI.get(str(job.get("status")), "❓")
    logs = [str(item) for item in job.get("logs", [])][-12:]
    log_text = "\n".join(f"• <code>{_h(line[-260:])}</code>" for line in logs)
    if not log_text:
        log_text = f"<i>{_h(job.get('message') or 'Нет сообщений')}</i>"
    return (
        f"{emoji} <b>Процесс <code>{_h(job['id'])}</code></b>\n\n"
        f"{_h(job.get('title') or 'Без названия')}\n"
        f"<code>{_progress_bar(int(job.get('progress') or 0))}</code>\n"
        f"{_h(job.get('message') or '')}\n\n"
        f"<b>Последние сообщения</b>\n{log_text}"
    )


async def _render_main(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_workflow(context)
    _state(context).pop("monitor_job", None)
    await _edit(
        message,
        "🎬 <b>Дорама за 5 — полный пульт</b>\n\n"
        "Telegram и веб-панель управляют одной очередью и одними настройками.",
        kb.main_menu(),
    )


async def _render_status(message: Message) -> None:
    dash = await _call_api(api.dashboard)
    stats = cast(api.JsonDict, dash["stats"])
    system = cast(api.JsonDict, dash["system"])
    platforms = cast(api.JsonDict, dash["platforms"])
    jobs = [job for job in dash.get("jobs", []) if isinstance(job, dict)]
    active = [job for job in jobs if job.get("status") in ACTIVE_STATUSES]
    youtube = cast(api.JsonDict, platforms["youtube"])
    lines = [
        "📊 <b>Состояние студии</b>\n",
        f"🟡 На проверке: <b>{stats['pending']}</b>",
        f"🟢 Опубликовано: <b>{stats['posted']}</b>",
        f"⚫ Отклонено: <b>{stats['rejected']}</b>",
        f"🔄 Активных процессов: <b>{len(active)}</b>",
        "",
        f"🤖 Ollama: {'✅' if system.get('ollama') else '❌'}",
        f"📅 Планировщик: {'✅' if system.get('scheduler') else '❌'}",
        f"✈ Telegram: {'✅' if system.get('telegram_bot') else '❌'}",
        f"▶ YouTube OAuth: {'✅' if youtube.get('connected') else '❌'}",
        f"⏰ Автопубликация: {'✅' if youtube.get('enabled') else '❌'}",
    ]
    if stats.get("next_publish"):
        lines.append(f"Следующий слот: {_h(stats['next_publish'])}")
    await _edit(message, "\n".join(lines), kb.back_main())


def _filtered_jobs(dashboard: api.JsonDict, filter_name: str) -> list[api.JsonDict]:
    jobs = [cast(api.JsonDict, job) for job in dashboard.get("jobs", []) if isinstance(job, dict)]
    if filter_name == "active":
        return [job for job in jobs if job.get("status") in ACTIVE_STATUSES]
    if filter_name == "errors":
        return [job for job in jobs if job.get("status") in {"failed", "attention"}]
    return jobs


async def _render_jobs(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
) -> None:
    state = _state(context)
    filter_name = str(state.get("jobs_filter") or "all")
    dashboard = await _call_api(api.dashboard)
    jobs = _filtered_jobs(dashboard, filter_name)
    state["jobs"] = jobs
    label = {"all": "все", "active": "активные", "errors": "ошибки"}.get(filter_name, filter_name)
    body = (
        f"🧭 <b>Процессы — {_h(label)}</b>\n\n"
        f"Найдено: <b>{len(jobs)}</b>. Состояние общее с веб-панелью."
        if jobs
        else f"🧭 <b>Процессы — {_h(label)}</b>\n\nВ этом фильтре пока пусто."
    )
    await _edit(message, body, kb.jobs_list(jobs, filter_name, page))


async def _render_job(message: Message, job_id: str) -> None:
    dashboard = await _call_api(api.dashboard)
    job = next(
        (cast(api.JsonDict, item) for item in dashboard.get("jobs", []) if isinstance(item, dict) and item.get("id") == job_id),
        None,
    )
    if job is None:
        await _edit(message, "❌ Процесс не найден в текущей истории.", kb.back_main())
        return
    running = str(job.get("status")) in ACTIVE_STATUSES
    await _edit(message, _job_text(job), kb.job_actions(job_id, running=running))


async def _render_queue(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
) -> None:
    state = _state(context)
    filter_status = str(state.get("queue_filter") or "all")
    clips = await _call_api(api.clips, None if filter_status == "all" else filter_status)
    state["queue_clips"] = clips
    label = {"all": "все", "pending": "на проверке", "posted": "опубликованные", "rejected": "отклонённые"}.get(filter_status, filter_status)
    text = f"📋 <b>Очередь публикаций — {_h(label)}</b>\n\nРоликов: <b>{len(clips)}</b>."
    if not clips:
        text += "\nВ этом разделе пока пусто."
    await _edit(
        message,
        text,
        kb.queue_list(clips, page=page, filter_status=filter_status),
    )


async def _render_clip(message: Message, clip_id: int) -> None:
    clip = await _call_api(api.clip, clip_id)
    caption = str(clip.get("caption") or "Без названия")
    title = caption.splitlines()[0][:300]
    body = (
        f"🎥 <b>Ролик #{clip_id}</b>\n\n"
        f"<b>{_h(title)}</b>\n"
        f"Статус: <b>{_h(_clip_status(clip.get('status')))}</b>\n"
        f"Файл: <code>{_h(clip.get('filename') or '—')}</code>\n"
        f"Размер: {_h(clip.get('size_mb') or 0)} МБ\n"
        f"Создан: {_h(clip.get('created_at') or '—')}"
    )
    await _edit(message, body, kb.clip_actions(clip))


async def _render_settings(message: Message) -> None:
    settings = await _call_api(api.pipeline_settings)
    body = (
        "⚙ <b>Настройки пайплайна</b>\n\n"
        f"🎙 Whisper: <code>{_h(settings['whisper_model'])}</code> / {_h(settings['whisper_device'])}\n"
        f"🤖 Ollama: <code>{_h(settings['ollama_model'])}</code>\n"
        f"🗣 Голос: <code>{_h(settings['voice'])}</code>\n"
        f"Скорость / тон: {_h(settings['rate'])} / {_h(settings['pitch'])}\n"
        f"⏱ Длительность: {_fmt_duration(settings['target_duration_seconds'])}\n"
        f"🎬 Сцен: {settings['scene_count']} · слов: {settings['target_script_words']}\n"
        f"🔉 Фон: {round(float(settings['original_audio_volume']) * 100)}%\n"
        f"✅ Ручная проверка: {'да' if settings['require_review'] else 'нет'}"
    )
    await _edit(message, body, kb.settings_main())


async def _render_setting_section(message: Message, section: str) -> None:
    settings = await _call_api(api.pipeline_settings)
    if section == "whisper":
        await _edit(
            message,
            f"🎙 <b>Whisper</b>\n\nМодель: <code>{_h(settings['whisper_model'])}</code>\nУстройство: <b>{_h(settings['whisper_device'])}</b>",
            kb.whisper_settings(str(settings["whisper_model"]), str(settings["whisper_device"])),
        )
    elif section == "ollama":
        await _edit(
            message,
            f"🤖 <b>Ollama</b>\n\nТекущая модель: <code>{_h(settings['ollama_model'])}</code>\n\nНажмите кнопку и пришлите имя другой установленной модели.",
            kb.confirm_action("settings:ollamaprompt", "open", "menu:settings"),
        )
    elif section == "voice":
        await _edit(
            message,
            f"🗣 <b>Озвучка</b>\n\nГолос: <code>{_h(settings['voice'])}</code>\nСкорость: {_h(settings['rate'])}\nТон: {_h(settings['pitch'])}",
            kb.voice_settings(str(settings["voice"]), str(settings["rate"]), str(settings["pitch"])),
        )
    elif section == "pipeline":
        await _edit(
            message,
            "🎛 <b>Монтаж и сценарий</b>\n\n"
            f"Длительность: {_fmt_duration(settings['target_duration_seconds'])}\n"
            f"Сцен: {settings['scene_count']}\n"
            f"Слов: {settings['target_script_words']}\n"
            f"Громкость фона: {round(float(settings['original_audio_volume']) * 100)}%",
            kb.pipeline_settings(settings),
        )
    elif section == "sources":
        selected = [str(item) for item in settings["search_sources"]]
        await _edit(
            message,
            "📡 <b>Источники радара</b>\n\nМинимум один источник должен остаться включённым.",
            kb.source_settings(selected),
        )


async def _render_youtube(message: Message) -> None:
    dashboard = await _call_api(api.dashboard)
    youtube = cast(api.JsonDict, cast(api.JsonDict, dashboard["platforms"])["youtube"])
    times = ", ".join(str(item) for item in youtube.get("post_times", [])) or "не заданы"
    body = (
        "▶ <b>YouTube и расписание</b>\n\n"
        f"OAuth: {'✅ подключён' if youtube.get('connected') else '❌ не подключён'}\n"
        f"Расписание: {'✅ включено' if youtube.get('enabled') else '❌ выключено'}\n"
        f"Слоты: <code>{_h(times)}</code>\n"
        f"Доступ по умолчанию: <b>{_h(youtube.get('privacy') or 'private')}</b>"
    )
    await _edit(
        message,
        body,
        kb.youtube_settings(bool(youtube.get("enabled")), str(youtube.get("privacy") or "private")),
    )


async def _save_pipeline_value(key: str, value: str) -> tuple[api.JsonDict, str]:
    settings = await _call_api(api.pipeline_settings)
    section = "pipeline"
    if key == "model":
        settings["whisper_model"] = value
        section = "whisper"
    elif key == "device":
        settings["whisper_device"] = value
        section = "whisper"
    elif key == "voice":
        settings["voice"] = "ru-RU-DmitryNeural" if value == "dmitry" else "ru-RU-SvetlanaNeural"
        section = "voice"
    elif key == "rate":
        settings["rate"] = {"p0": "+0%", "p8": "+8%", "p12": "+12%", "p18": "+18%"}[value]
        section = "voice"
    elif key == "pitch":
        settings["pitch"] = {"m4": "-4Hz", "m2": "-2Hz", "p0": "+0Hz", "p2": "+2Hz"}[value]
        section = "voice"
    elif key == "duration":
        settings["target_duration_seconds"] = int(value)
    elif key == "scenes":
        settings["scene_count"] = int(value)
    elif key == "words":
        settings["target_script_words"] = int(value)
    elif key == "volume":
        settings["original_audio_volume"] = int(value) / 100
    elif key == "review":
        settings["require_review"] = not bool(settings["require_review"])
    elif key == "source":
        sources = [str(item) for item in settings["search_sources"]]
        if value in sources:
            if len(sources) == 1:
                raise ValueError("Нельзя отключить последний источник")
            sources.remove(value)
        else:
            sources.append(value)
        settings["search_sources"] = sources
        section = "sources"
    else:
        raise ValueError("Неизвестная настройка")
    await _call_api(api.save_pipeline_settings, settings)
    return settings, section


def _parse_clock(raw: str) -> float:
    value = raw.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        raise ValueError("Используйте ММ:СС или ЧЧ:ММ:СС")
    numbers = [int(part) for part in parts]
    if numbers[-1] >= 60 or numbers[-2] >= 60:
        raise ValueError("Секунды и минуты должны быть меньше 60")
    if len(numbers) == 2:
        return float(numbers[0] * 60 + numbers[1])
    return float(numbers[0] * 3600 + numbers[1] * 60 + numbers[2])


def _parse_interval(raw: str, duration: float) -> tuple[float, float]:
    normalized = raw.replace("—", "-").replace("–", "-")
    parts = [part.strip() for part in normalized.split("-", 1)]
    if len(parts) != 2:
        raise ValueError("Введите интервал в виде 04:12-09:12")
    start, end = _parse_clock(parts[0]), _parse_clock(parts[1])
    selected = end - start
    if start < 0 or end > duration + 0.25:
        raise ValueError("Интервал выходит за длительность видео")
    if selected < 10:
        raise ValueError("Минимальная длительность — 10 секунд")
    if selected > 300.01:
        raise ValueError("Максимальная длительность перевода — 5 минут")
    return start, min(end, duration)


async def _episode_ready(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    metadata: api.JsonDict,
    mode: str,
) -> None:
    state = _state(context)
    state["episode"] = {
        "filename": str(metadata["filename"]),
        "display_name": str(metadata.get("display_name") or metadata["filename"]),
        "duration_seconds": float(metadata["duration_seconds"]),
        "mode": mode,
        "focus": "",
        "start_seconds": 0.0,
        "end_seconds": None,
    }
    duration = float(metadata["duration_seconds"])
    if mode == "recap":
        state["awaiting"] = "episode_focus"
        await _edit_control(
            message,
            context,
            f"🎞 <b>Файл принят</b>\n\n<code>{_h(metadata['display_name'])}</code>\n"
            f"Длительность: {_fmt_duration(duration)} · {metadata.get('size_mb', '?')} МБ\n\n"
            "Пришлите фокус пересказа или выберите вариант без фокуса.",
            kb.episode_focus(),
        )
    else:
        state["awaiting"] = "translation_interval"
        await _edit_control(
            message,
            context,
            f"🌐 <b>Файл принят для прямого перевода</b>\n\n"
            f"<code>{_h(metadata['display_name'])}</code>\nДлительность: {_fmt_duration(duration)}\n\n"
            "Выберите готовый интервал или пришлите свой: <code>04:12-09:12</code>. Максимум 5 минут.",
            kb.translation_interval(duration),
        )


async def _start_job_view(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    job: api.JsonDict,
) -> None:
    job_id = str(job.get("id") or "?")
    _state(context)["monitor_job"] = job_id
    await _edit(
        message,
        f"🚀 <b>Процесс запущен</b>\n\n<code>{_h(job_id)}</code>\n{_h(job.get('title') or '')}\n"
        f"<code>{_progress_bar(int(job.get('progress') or 0))}</code>",
        kb.job_actions(job_id, running=True),
    )


async def _send_clip_preview(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    clip_id: int,
) -> None:
    status_message = await message.reply_text("⏳ Готовлю Telegram-предпросмотр…")
    try:
        clip = await _call_api(api.clip, clip_id)
        with tempfile.TemporaryDirectory(prefix="dorama-telegram-preview-") as temporary:
            temp_dir = Path(temporary)
            source = await _call_api(
                api.download_clip,
                clip_id,
                temp_dir / Path(str(clip.get("filename") or f"clip-{clip_id}.mp4")).name,
            )
            preview = await _call_api(prepare_telegram_preview, source, temp_dir)
            await context.bot.send_video(
                chat_id=message.chat_id,
                video=preview,
                caption=f"Предпросмотр ролика #{clip_id}",
                supports_streaming=True,
                read_timeout=180,
                write_timeout=900,
                connect_timeout=20,
                pool_timeout=20,
            )
        await status_message.edit_text("✅ Предпросмотр отправлен")
    except Exception as exc:
        await status_message.edit_text(
            "❌ Не удалось отправить предпросмотр в Telegram. "
            f"Ролик остаётся доступен в веб-панели.\n{str(exc)[:800]}"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None:
        return
    sent = await message.reply_text(
        "🎬 <b>Дорама за 5 — полный пульт</b>\n\n"
        "Все функции работают через общий API веб-панели.",
        parse_mode="HTML",
        reply_markup=kb.main_menu(),
    )
    _set_control(sent, context)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    if cb is None:
        return
    if not _auth(update):
        await cb.answer("Нет доступа", show_alert=True)
        return
    data = cb.data or ""
    raw_message = cb.message
    if raw_message is None or not hasattr(raw_message, "edit_text"):
        await cb.answer(text="Сообщение больше недоступно", show_alert=True)
        return
    message = cast(Message, raw_message)
    state = _state(context)
    _set_control(message, context)
    if not data.startswith("job:log:"):
        state.pop("monitor_job", None)

    if data == "noop":
        await _answer(cb)
        return
    try:
        if data == "menu:main":
            await _answer(cb)
            await _render_main(message, context)
            return
        if data == "menu:status":
            await _answer(cb, "Обновляю…")
            await _render_status(message)
            return
        if data == "menu:create":
            await _answer(cb)
            _clear_workflow(context)
            await _edit(message, "🎬 <b>Создать выпуск</b>\n\nВыберите режим:", kb.create_menu())
            return
        if data == "menu:jobs":
            await _answer(cb, "Загружаю…")
            state.setdefault("jobs_filter", "all")
            await _render_jobs(message, context)
            return
        if data == "menu:queue":
            await _answer(cb, "Загружаю…")
            state.setdefault("queue_filter", "all")
            await _render_queue(message, context)
            return
        if data == "menu:settings":
            await _answer(cb, "Загружаю…")
            await _render_settings(message)
            return
        if data == "menu:youtube":
            await _answer(cb, "Загружаю…")
            await _render_youtube(message)
            return

        if data.startswith(("create:dorama", "create:licensed")):
            await _answer(cb)
            job_type = data.split(":", 1)[1]
            state["awaiting"] = "create_query"
            state["create_type"] = job_type
            state["create_limit"] = 10
            label = "Дорама-радар" if job_type == "dorama" else "Автопоиск разрешённого CC-видео"
            await _edit(message, f"📡 <b>{label}</b>\n\nПришлите тему выпуска.", kb.create_menu())
            return
        if data.startswith("create:limit:"):
            await _answer(cb)
            _, _, job_type, limit_raw = data.split(":")
            state["create_type"] = job_type
            state["create_limit"] = int(limit_raw)
            query = str(state.get("create_query") or "")
            await _edit(
                message,
                f"📝 Тема: <b>{_h(query)}</b>\nРезультатов на источник: <b>{limit_raw}</b>\n\nЗапустить?",
                kb.create_options(job_type, int(limit_raw)),
            )
            return
        if data.startswith(("create:upload:", "create:path:")):
            await _answer(cb)
            kind, mode = data.split(":")[1:]
            state["episode_mode"] = mode
            if kind == "upload":
                state["awaiting"] = "telegram_video"
                text = (
                    "📎 <b>Пришлите видео</b> как видео или документ.\n\n"
                    "После загрузки файл пройдёт тот же валидатор, что и в веб-панели. "
                    "Если Telegram не принимает большой файл, положите его в <code>input</code> и выберите соответствующий пункт."
                )
            else:
                state["awaiting"] = "input_filename"
                text = "📁 <b>Файл из input</b>\n\nПришлите точное имя файла с расширением."
            await _edit(message, text, kb.create_menu())
            return
        if data.startswith("run:"):
            await _answer(cb, "Запускаю…")
            job_type = data.split(":", 1)[1]
            query = str(state.get("create_query") or "").strip()
            result_limit = int(state.get("create_limit") or 10)
            if not query:
                raise ValueError("Тема устарела — введите её заново")
            if job_type == "dorama":
                result = await _call_api(api.start_dorama, query, result_limit)
            elif job_type == "licensed":
                result = await _call_api(
                    api.start_licensed,
                    query,
                    "динамичный последовательный пересказ без раскрытия финала",
                    result_limit,
                )
            else:
                raise ValueError("Неизвестный режим создания")
            _clear_workflow(context)
            await _start_job_view(message, context, result)
            return

        if data == "episode:focus:skip":
            await _answer(cb)
            episode = cast(dict[str, Any], state.get("episode") or {})
            episode["focus"] = ""
            state["episode"] = episode
            state.pop("awaiting", None)
            await _edit(
                message,
                f"🎞 <b>Пересказ готов к запуску</b>\n\n<code>{_h(episode.get('display_name'))}</code>\n"
                "Фокус: без дополнительного ограничения.",
                kb.confirm_episode("recap"),
            )
            return
        if data.startswith("episode:interval:"):
            await _answer(cb)
            episode = cast(dict[str, Any], state.get("episode") or {})
            duration = float(episode.get("duration_seconds") or 0)
            tail = data.removeprefix("episode:interval:")
            if tail == "full":
                start_seconds, end_seconds = 0.0, duration
            else:
                start_raw, end_raw = tail.split(":", 1)
                start_seconds, end_seconds = float(start_raw), float(end_raw)
            start_seconds, end_seconds = _parse_interval(
                f"{start_seconds}-{end_seconds}", duration
            )
            episode["start_seconds"] = start_seconds
            episode["end_seconds"] = end_seconds
            state["episode"] = episode
            state.pop("awaiting", None)
            await _edit(
                message,
                f"🌐 <b>Перевод готов к запуску</b>\n\n<code>{_h(episode.get('display_name'))}</code>\n"
                f"Интервал: <b>{_fmt_duration(start_seconds)}–{_fmt_duration(end_seconds)}</b>",
                kb.confirm_episode("translate"),
            )
            return
        if data.startswith("episode:run:"):
            await _answer(cb, "Запускаю…")
            mode = data.split(":")[-1]
            episode = cast(dict[str, Any], state.get("episode") or {})
            if not episode.get("filename"):
                raise ValueError("Файл запуска устарел — выберите его заново")
            result = await _call_api(
                api.start_episode,
                str(episode["filename"]),
                str(episode.get("focus") or ""),
                mode=mode,
                start_seconds=float(episode.get("start_seconds") or 0),
                end_seconds=(float(episode["end_seconds"]) if episode.get("end_seconds") is not None else None),
            )
            _clear_workflow(context)
            await _start_job_view(message, context, result)
            return

        if data.startswith("queue:filters:"):
            await _answer(cb)
            selected = data.split(":")[-1]
            await _edit(message, "📋 <b>Фильтр очереди</b>", kb.queue_filters(selected))
            return
        if data.startswith("queue:filter:"):
            await _answer(cb, "Фильтрую…")
            state["queue_filter"] = data.split(":")[-1]
            await _render_queue(message, context)
            return
        if data.startswith("queue:page:"):
            await _answer(cb)
            await _render_queue(message, context, page=int(data.split(":")[-1]))
            return
        if data.startswith("clip:detail:"):
            await _answer(cb, "Обновляю…")
            await _render_clip(message, int(data.split(":")[-1]))
            return
        if data.startswith("clip:preview:"):
            await _answer(cb, "Готовлю…")
            await _send_clip_preview(message, context, int(data.split(":")[-1]))
            return
        if data.startswith("clip:edit:"):
            await _answer(cb)
            clip_id = int(data.split(":")[-1])
            clip = await _call_api(api.clip, clip_id)
            state["awaiting"] = "edit_caption"
            state["edit_clip_id"] = clip_id
            await _edit(
                message,
                f"✏ <b>Текст и хештеги ролика #{clip_id}</b>\n\n"
                f"Текущий вариант:\n<pre>{_h(str(clip.get('caption') or '')[:2600])}</pre>\n"
                "Пришлите новый полный текст одним сообщением.",
                kb.clip_actions(clip),
            )
            return
        if data.startswith("clip:publish:"):
            await _answer(cb)
            clip_id = int(data.split(":")[-1])
            await _edit(
                message,
                f"▶ <b>Публикация ролика #{clip_id}</b>\n\nВыберите уровень доступа:",
                kb.publish_privacy(clip_id),
            )
            return
        if data.startswith("clip:yt:"):
            await _answer(cb, "Ставлю в очередь…")
            legacy_clip_id = int(data.split(":")[-1])
            result = await _call_api(api.publish_youtube, legacy_clip_id, "private")
            await _start_job_view(message, context, result)
            return
        if data.startswith("clip:publicconfirm:"):
            await _answer(cb)
            clip_id = int(data.split(":")[-1])
            await _edit(
                message,
                "⚠️ <b>Публичная публикация</b>\n\nРолик сразу станет виден подписчикам. Продолжить?",
                kb.confirm_action("clip:privacypublic", clip_id, f"clip:publish:{clip_id}"),
            )
            return
        if data.startswith("clip:privacypublic:"):
            privacy, clip_id = "public", int(data.split(":")[-1])
        elif data.startswith("clip:privacy:"):
            _, _, clip_raw, privacy = data.split(":")
            clip_id = int(clip_raw)
        else:
            privacy = ""
            clip_id = 0
        if privacy:
            await _answer(cb, "Ставлю в очередь…")
            result = await _call_api(api.publish_youtube, clip_id, privacy)
            await _start_job_view(message, context, result)
            return
        if data.startswith("clip:rejectconfirm:"):
            await _answer(cb)
            clip_id = int(data.split(":")[-1])
            await _edit(
                message,
                f"↘ <b>Отклонить ролик #{clip_id}?</b>\n\nФайл будет перенесён в rejected.",
                kb.confirm_action("clip:reject", clip_id, f"clip:detail:{clip_id}"),
            )
            return
        if data.startswith("clip:reject:"):
            await _answer(cb, "Отклоняю…")
            clip_id = int(data.split(":")[-1])
            await _call_api(api.reject_clip, clip_id)
            await _render_clip(message, clip_id)
            return
        if data.startswith("clip:deleteconfirm:"):
            await _answer(cb)
            clip_id = int(data.split(":")[-1])
            await _edit(
                message,
                f"🗑 <b>Удалить ролик #{clip_id} полностью?</b>\n\n"
                "Будут удалены запись очереди, локальное видео, субтитры и служебные файлы.",
                kb.confirm_action("clip:delete", clip_id, f"clip:detail:{clip_id}"),
            )
            return
        if data.startswith("clip:delete:"):
            await _answer(cb, "Удаляю…")
            await _call_api(api.delete_clip, int(data.split(":")[-1]))
            await _render_queue(message, context)
            return

        if data.startswith("jobs:filter:"):
            await _answer(cb, "Фильтрую…")
            state["jobs_filter"] = data.split(":")[-1]
            await _render_jobs(message, context)
            return
        if data.startswith("jobs:page:"):
            await _answer(cb)
            await _render_jobs(message, context, page=int(data.split(":")[-1]))
            return
        if data == "jobs:clearconfirm":
            await _answer(cb)
            await _edit(
                message,
                "🧹 <b>Очистить завершённые процессы?</b>\n\n"
                "Ошибки и успешные процессы исчезнут из истории. Активные задачи сохранятся.",
                kb.confirm_action("jobs:clear", "yes", "menu:jobs"),
            )
            return
        if data == "jobs:clear:yes":
            await _answer(cb, "Очищаю…")
            result = await _call_api(api.clear_job_history)
            await _edit(
                message,
                f"✅ Удалено завершённых процессов: <b>{int(result.get('cleared') or 0)}</b>",
                kb.back_main(),
            )
            return
        if data.startswith("job:log:"):
            await _answer(cb, "Обновляю…")
            await _render_job(message, data.split(":")[-1])
            return
        if data.startswith("job:cancelconfirm:"):
            await _answer(cb)
            job_id = data.split(":")[-1]
            await _edit(
                message,
                f"✖ <b>Отменить процесс <code>{_h(job_id)}</code>?</b>",
                kb.confirm_action("job:cancel", job_id, f"job:log:{job_id}"),
            )
            return
        if data.startswith("job:cancel:"):
            await _answer(cb, "Отменяю…")
            job_id = data.split(":")[-1]
            await _call_api(api.cancel_job, job_id)
            await _render_job(message, job_id)
            return

        if data.startswith("settings:") and data.count(":") == 1:
            await _answer(cb, "Загружаю…")
            await _render_setting_section(message, data.split(":")[-1])
            return
        if data == "settings:ollamaprompt:open":
            await _answer(cb)
            state["awaiting"] = "ollama_model"
            await _edit(
                message,
                "🤖 <b>Модель Ollama</b>\n\nПришлите имя установленной модели, например <code>qwen2.5:7b</code>.",
                kb.back_main(),
            )
            return
        if data.startswith("pset:"):
            await _answer(cb, "Сохраняю…")
            _, key, value = data.split(":", 2)
            _, section = await _save_pipeline_value(key, value)
            await _render_setting_section(message, section)
            return

        if data == "yt:toggle":
            await _answer(cb, "Сохраняю…")
            dashboard = await _call_api(api.dashboard)
            youtube = cast(api.JsonDict, cast(api.JsonDict, dashboard["platforms"])["youtube"])
            await _call_api(
                api.save_youtube_settings,
                enabled=not bool(youtube.get("enabled")),
                post_times=[str(item) for item in youtube.get("post_times", [])] or ["12:00"],
                privacy_status=str(youtube.get("privacy") or "private"),
            )
            await _render_youtube(message)
            return
        if data.startswith("yt:privacy:"):
            await _answer(cb, "Сохраняю…")
            privacy = data.split(":")[-1]
            dashboard = await _call_api(api.dashboard)
            youtube = cast(api.JsonDict, cast(api.JsonDict, dashboard["platforms"])["youtube"])
            await _call_api(
                api.save_youtube_settings,
                enabled=bool(youtube.get("enabled")),
                post_times=[str(item) for item in youtube.get("post_times", [])] or ["12:00"],
                privacy_status=privacy,
            )
            await _render_youtube(message)
            return
        if data == "yt:publicconfirm":
            await _answer(cb)
            await _edit(
                message,
                "⚠️ <b>Публичность по умолчанию</b>\n\n"
                "Будущие автоматические публикации смогут сразу стать публичными. Включить?",
                kb.confirm_action("yt:privacypublic", "yes", "menu:youtube"),
            )
            return
        if data == "yt:privacypublic:yes":
            await _answer(cb, "Сохраняю…")
            dashboard = await _call_api(api.dashboard)
            youtube = cast(api.JsonDict, cast(api.JsonDict, dashboard["platforms"])["youtube"])
            await _call_api(
                api.save_youtube_settings,
                enabled=bool(youtube.get("enabled")),
                post_times=[str(item) for item in youtube.get("post_times", [])] or ["12:00"],
                privacy_status="public",
            )
            await _render_youtube(message)
            return
        if data == "yt:times":
            await _answer(cb)
            state["awaiting"] = "youtube_times"
            await _edit(
                message,
                "🕐 <b>Слоты YouTube</b>\n\nПришлите от 1 до 6 значений через запятую: <code>12:00, 19:00</code>.",
                kb.back_main(),
            )
            return

        if data == "menu:doctor":
            await _answer(cb, "Запускаю…")
            result = await _call_api(api.run_doctor)
            await _start_job_view(message, context, result)
            return
        if data == "menu:scheduler":
            await _answer(cb, "Проверяю…")
            result = await _call_api(api.run_scheduler)
            await _start_job_view(message, context, result)
            return

        await _answer(cb, "Неизвестная команда")
    except Exception as exc:
        log.exception("Ошибка Telegram callback %s", data)
        await _edit(
            message,
            f"❌ <b>Не удалось выполнить действие</b>\n\n{_h(str(exc)[:1600])}",
            kb.back_main(),
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None or message.text is None:
        return
    state = _state(context)
    text = message.text.strip()
    awaiting = str(state.get("awaiting") or "")
    if not awaiting:
        return
    try:
        if awaiting == "create_query":
            if len(text) < 3:
                raise ValueError("Тема должна содержать минимум 3 символа")
            state["create_query"] = text[:180]
            state["awaiting"] = ""
            job_type = str(state.get("create_type") or "dorama")
            limit = int(state.get("create_limit") or 10)
            await _edit_control(
                message,
                context,
                f"📝 Тема: <b>{_h(state['create_query'])}</b>\n"
                f"Результатов на источник: <b>{limit}</b>\n\nЗапустить?",
                kb.create_options(job_type, limit),
            )
        elif awaiting == "input_filename":
            metadata = await _call_api(api.upload_metadata, Path(text).name)
            await _episode_ready(message, context, metadata, str(state.get("episode_mode") or "recap"))
        elif awaiting == "episode_focus":
            episode = cast(dict[str, Any], state.get("episode") or {})
            episode["focus"] = text[:300]
            state["episode"] = episode
            state.pop("awaiting", None)
            await _edit_control(
                message,
                context,
                f"🎞 <b>Пересказ готов к запуску</b>\n\n<code>{_h(episode.get('display_name'))}</code>\n"
                f"Фокус: {_h(episode['focus'])}",
                kb.confirm_episode("recap"),
            )
        elif awaiting == "translation_interval":
            episode = cast(dict[str, Any], state.get("episode") or {})
            start_seconds, end_seconds = _parse_interval(
                text, float(episode.get("duration_seconds") or 0)
            )
            episode["start_seconds"] = start_seconds
            episode["end_seconds"] = end_seconds
            state["episode"] = episode
            state.pop("awaiting", None)
            await _edit_control(
                message,
                context,
                f"🌐 <b>Перевод готов к запуску</b>\n\n"
                f"Интервал: <b>{_fmt_duration(start_seconds)}–{_fmt_duration(end_seconds)}</b>",
                kb.confirm_episode("translate"),
            )
        elif awaiting == "edit_caption":
            clip_id = int(state["edit_clip_id"])
            await _call_api(api.edit_caption, clip_id, text[:5000])
            state.pop("awaiting", None)
            state.pop("edit_clip_id", None)
            clip = await _call_api(api.clip, clip_id)
            await _edit_control(
                message,
                context,
                f"✅ Текст и хештеги ролика #{clip_id} обновлены.",
                kb.clip_actions(clip),
            )
        elif awaiting == "ollama_model":
            settings = await _call_api(api.pipeline_settings)
            settings["ollama_model"] = text[:60]
            await _call_api(api.save_pipeline_settings, settings)
            state.pop("awaiting", None)
            await _edit_control(
                message,
                context,
                f"✅ Модель Ollama сохранена: <code>{_h(settings['ollama_model'])}</code>",
                kb.settings_main(),
            )
        elif awaiting == "youtube_times":
            times = [item.strip() for item in text.split(",") if item.strip()]
            if not 1 <= len(times) <= 6 or any(
                not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) for item in times
            ):
                raise ValueError("Используйте формат 12:00, 19:00 — от 1 до 6 слотов")
            dashboard = await _call_api(api.dashboard)
            youtube = cast(api.JsonDict, cast(api.JsonDict, dashboard["platforms"])["youtube"])
            await _call_api(
                api.save_youtube_settings,
                enabled=bool(youtube.get("enabled")),
                post_times=times,
                privacy_status=str(youtube.get("privacy") or "private"),
            )
            state.pop("awaiting", None)
            await _edit_control(
                message,
                context,
                f"✅ Слоты сохранены: <code>{_h(', '.join(times))}</code>",
                kb.youtube_settings(bool(youtube.get("enabled")), str(youtube.get("privacy") or "private")),
            )
    except Exception as exc:
        await message.reply_text(
            f"❌ {str(exc)[:1500]}\n\nИсправьте значение или используйте /cancel.",
            reply_markup=kb.back_main(),
        )


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None:
        return
    state = _state(context)
    if state.get("awaiting") != "telegram_video":
        await message.reply_text("Сначала выберите «Создать → Пересказ/Перевод файла».", reply_markup=kb.main_menu())
        return
    attachment = message.video or message.document
    if attachment is None:
        return
    filename = Path(getattr(attachment, "file_name", "") or f"telegram-{attachment.file_unique_id}.mp4").name
    progress = await message.reply_text("⬇ Загружаю видео из Telegram на локальный сервер…")
    try:
        with tempfile.TemporaryDirectory(prefix="dorama-telegram-upload-") as temporary:
            local_path = Path(temporary) / filename
            telegram_file = await attachment.get_file()
            await telegram_file.download_to_drive(local_path)
            await progress.edit_text("🔎 Проверяю контейнер и длительность…")
            metadata = await _call_api(api.upload_video, local_path)
        await progress.edit_text("✅ Файл сохранён в общей папке input")
        await _episode_ready(
            message,
            context,
            metadata,
            str(state.get("episode_mode") or "recap"),
        )
    except TelegramError as exc:
        await progress.edit_text(
            "❌ Telegram не отдал файл боту. Для большого видео положите файл в папку input "
            f"и выберите режим «Из input».\n{str(exc)[:900]}"
        )
    except Exception as exc:
        await progress.edit_text(f"❌ Не удалось принять видео: {str(exc)[:1200]}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None:
        return
    _clear_workflow(context)
    sent = await message.reply_text("↩ Действие отменено.", reply_markup=kb.main_menu())
    _set_control(sent, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "<b>Команды</b>\n/start или /menu — открыть пульт\n/cancel — отменить текущий ввод\n/help — эта справка\n\n"
        "Все остальные действия доступны кнопками.",
        parse_mode="HTML",
        reply_markup=kb.main_menu(),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Необработанная ошибка Telegram", exc_info=context.error)


def _build_application(token: str) -> Application[Any, Any, Any, Any, Any, Any]:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler(["start", "menu"], start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, on_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(error_handler)
    return application


def run_bot_thread(token: str, user_id: int) -> Callable[[], None]:
    """Запустить один управляемый polling-поток и вернуть функцию остановки."""
    global BOT_TOKEN, ALLOWED_USER
    BOT_TOKEN = token
    ALLOWED_USER = user_id
    global _bot_thread, _bot_stop_event

    with _bot_lock:
        if _bot_thread is not None and _bot_thread.is_alive():
            return stop_bot_thread
        stop_event = ThreadEvent()
        _bot_stop_event = stop_event

        async def _run_bot() -> None:
            application = _build_application(BOT_TOKEN)
            initialized = started = polling = False
            try:
                await application.initialize()
                initialized = True
                await application.start()
                started = True
                if application.updater is None:
                    raise RuntimeError("Telegram updater недоступен")
                await application.updater.start_polling(drop_pending_updates=True)
                polling = True
                log.info("Telegram-бот запущен (user_id=%s)", ALLOWED_USER)
                while not stop_event.is_set():
                    await asyncio.sleep(0.25)
            finally:
                if polling and application.updater is not None:
                    await application.updater.stop()
                if started:
                    await application.stop()
                if initialized:
                    await application.shutdown()

        def _start() -> None:
            try:
                asyncio.run(_run_bot())
            except Exception:
                log.exception("Telegram-бот остановлен из-за ошибки")

        _bot_thread = Thread(target=_start, daemon=True, name="telegram-bot")
        _bot_thread.start()
        log.info("Поток бота запущен")
    return stop_bot_thread


def stop_bot_thread(timeout: float = 10.0) -> None:
    global _bot_thread, _bot_stop_event
    with _bot_lock:
        thread = _bot_thread
        stop_event = _bot_stop_event
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    with _bot_lock:
        if _bot_thread is thread and (thread is None or not thread.is_alive()):
            _bot_thread = None
            _bot_stop_event = None


def bot_is_running() -> bool:
    with _bot_lock:
        return _bot_thread is not None and _bot_thread.is_alive()


def run_bot() -> None:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN не задан — бот не запускается")
        return
    if not ALLOWED_USER:
        log.error("TELEGRAM_USER_ID не задан — бот не запускается")
        return
    application = _build_application(BOT_TOKEN)
    log.info("Бот запущен")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
