"""Telegram-бот: удалённое управление студией.

Паттерн: одно сообщение, edit_message_text при каждой навигации.
Нет спама — кнопки редактируют текущее сообщение.
"""
# ruff: noqa: BLE001,S110 — обработчики являются внешней UI-границей Telegram
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Lock, Thread
from typing import Any, ParamSpec, TypeVar, cast

from telegram import CallbackQuery, Message, Update
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
_bot_thread: Thread | None = None
_bot_stop_event: ThreadEvent | None = None
_bot_lock = Lock()
P = ParamSpec("P")
T = TypeVar("T")


def _progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled) + f" {pct}%"


def _auth(update: Update) -> bool:
    return bool(ALLOWED_USER and update.effective_user and update.effective_user.id == ALLOWED_USER)


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


async def _answer(cb: CallbackQuery, text: str | None = None) -> None:
    try:
        await cb.answer(text=text, show_alert=False)
    except Exception:
        pass


async def _call_api(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Не блокировать event loop Telegram синхронным localhost HTTP-запросом."""
    return await asyncio.to_thread(function, *args, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "🎬 *Дорама за 5* — пульт управления\n\n"
        "Всё в этом сообщении. Кнопки меняют содержимое.",
        parse_mode="Markdown",
        reply_markup=kb.main_menu(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    if cb is None:
        return
    if not _auth(update):
        await cb.answer("Нет доступа", show_alert=True)
        return

    data = cb.data or ""
    msg = cb.message
    if msg is None or not hasattr(msg, "edit_text"):
        await cb.answer(text="Сообщение больше недоступно", show_alert=True)
        return
    msg = cast(Message, msg)
    if context.user_data is None:
        await cb.answer("Сессия недоступна", show_alert=True)
        return

    if data == "noop":
        await _answer(cb)
        return

    if data == "menu:main":
        await _answer(cb)
        context.user_data.pop("create_type", None)
        try:
            await msg.edit_text(
                "🎬 *Дорама за 5* — пульт управления\n\n"
                "Выберите раздел:",
                parse_mode="Markdown",
                reply_markup=kb.main_menu(),
            )
        except Exception:
            pass
        return

    if data == "menu:status":
        await _answer(cb, "Загружаю…")
        try:
            dash = await _call_api(api.dashboard)
        except Exception as e:
            await msg.edit_text(f"❌ API недоступен: {e}", reply_markup=kb.back_main())
            return
        stats = dash["stats"]
        system = dash["system"]
        jobs = dash.get("jobs", [])
        active = [
            j
            for j in jobs
            if j["status"] in ("running", "queued", "cancelling", "paused")
        ]
        attention = [j for j in jobs if j["status"] == "attention"]
        lines = [
            "📊 *Статус системы*\n",
            f"📂 В очереди: *{stats['pending']}*  ✅ Опубликовано: *{stats['posted']}*",
            f"🤖 Ollama: {'✅' if system['ollama'] else '❌'}  📅 Планировщик: {'✅' if system['scheduler'] else '❌'}",
        ]
        if stats.get("next_publish"):
            lines.append(f"⏭ Следующий слот: {stats['next_publish']}")
        if active:
            lines.append(f"\n🔄 *Активные задачи ({len(active)}):*")
            for j in active[:5]:
                emoji = STATUS_EMOJI.get(j["status"], "❓")
                lines.append(f"  {emoji} `{j['id']}` {j['title'][:40]}  {_progress_bar(j['progress'])}")
        else:
            lines.append("\n⏸ Активных задач нет")
        if attention:
            lines.append(
                f"\n⚠️ Требуют проверки после перезапуска: *{len(attention)}*"
            )
            for job in attention[:3]:
                lines.append(f"  ⚠️ `{job['id']}` {job['title'][:40]}")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb.back_main())
        return

    if data == "menu:create":
        await _answer(cb)
        await msg.edit_text(
            "🎬 *Создать выпуск*\n\nВыберите тип:",
            parse_mode="Markdown",
            reply_markup=kb.create_menu(),
        )
        return

    if data == "menu:settings":
        await _answer(cb, "Загружаю…")
        try:
            s = await _call_api(api.pipeline_settings)
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb.back_main())
            return
        lines = [
            "⚙ *Настройки пайплайна*\n",
            f"🎙 Whisper: `{s['whisper_model']}` ({s['whisper_device']})",
            f"🤖 Ollama: `{s['ollama_model']}`",
            f"🗣 Голос: `{s['voice']}`  Скорость: `{s['rate']}`  Тон: `{s['pitch']}`",
            f"⏱ Длительность: *{_fmt_duration(s['target_duration_seconds'])}*  Сцен: *{s['scene_count']}*",
            f"📝 Слов в сценарии: *{s['target_script_words']}*  Громкость оригинала: *{s['original_audio_volume']}*",
            f"📡 Источники: {', '.join(s['search_sources'])}",
            f"✅ Ручная проверка: {'да' if s['require_review'] else 'нет'}",
        ]
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb.settings_view())
        return

    if data == "menu:queue":
        await _answer(cb, "Загружаю…")
        try:
            clips = await _call_api(api.pending_clips)
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb.back_main())
            return
        if not clips:
            await msg.edit_text("📋 *Очередь пуста*\n\nНет роликов на проверке.", parse_mode="Markdown", reply_markup=kb.back_main())
            return
        context.user_data["queue_clips"] = clips
        await msg.edit_text(
            f"📋 *Очередь публикации* ({len(clips)} роликов)\n\nВыберите ролик:",
            parse_mode="Markdown",
            reply_markup=kb.queue_list(clips, page=0),
        )
        return

    if data.startswith("queue:page:"):
        await _answer(cb)
        page = int(data.split(":")[-1])
        clips = context.user_data.get("queue_clips", [])
        await msg.edit_text(
            f"📋 *Очередь публикации* ({len(clips)} роликов)\n\nВыберите ролик:",
            parse_mode="Markdown",
            reply_markup=kb.queue_list(clips, page=page),
        )
        return

    if data.startswith("clip:detail:"):
        await _answer(cb)
        clip_id = int(data.split(":")[-1])
        clips = context.user_data.get("queue_clips", [])
        clip = next((c for c in clips if c["id"] == clip_id), None)
        if not clip:
            await msg.edit_text("Ролик не найден", reply_markup=kb.back_main())
            return
        cap = clip["caption"][:120]
        lines = [
            f"🎥 *Ролик #{clip_id}*\n",
            f"📝 {cap}",
            f"📏 {clip.get('size_mb', '?')} MB  📁 `{clip.get('filename', '?')}`",
            f"🕐 {clip.get('created_at', '?')}",
        ]
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb.clip_actions(clip_id))
        return

    if data.startswith("clip:yt:"):
        await _answer(cb, "Загружаю…")
        clip_id = int(data.split(":")[-1])
        try:
            result = await _call_api(api.publish_youtube, clip_id, "private")
            job_id = result.get("id", "?")
            await msg.edit_text(
                f"✅ Ролик #{clip_id} отправлен на YouTube\n"
                f"🆔 Задача: `{job_id}`",
                parse_mode="Markdown",
                reply_markup=kb.job_actions(job_id, running=True),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка YouTube: {e}", reply_markup=kb.back_main())
        return

    if data.startswith("clip:reject:"):
        await _answer(cb, "Отклоняю…")
        clip_id = int(data.split(":")[-1])
        try:
            await _call_api(api.reject_clip, clip_id)
            await msg.edit_text(f"🗑 Ролик #{clip_id} отклонён", parse_mode="Markdown", reply_markup=kb.back_main())
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb.back_main())
        return

    if data.startswith("create:dorama"):
        await _answer(cb)
        context.user_data["create_type"] = "dorama"
        context.user_data["control_chat_id"] = msg.chat_id
        context.user_data["control_message_id"] = msg.message_id
        await msg.edit_text(
            "📡 *Дорама-радар*\n\nВведите тему выпуска (или /cancel):",
            parse_mode="Markdown",
            reply_markup=kb.back_main(),
        )
        return

    if data.startswith("create:licensed"):
        await _answer(cb)
        context.user_data["create_type"] = "licensed"
        context.user_data["control_chat_id"] = msg.chat_id
        context.user_data["control_message_id"] = msg.message_id
        await msg.edit_text(
            "🎓 *Лицензия CC*\n\nВведите тему поиска (или /cancel):",
            parse_mode="Markdown",
            reply_markup=kb.back_main(),
        )
        return

    if data.startswith("create:episode"):
        await _answer(cb)
        context.user_data["create_type"] = "episode"
        context.user_data["control_chat_id"] = msg.chat_id
        context.user_data["control_message_id"] = msg.message_id
        await msg.edit_text(
            "🎞 *Серия → 5 мин*\n\nВведите название файла из папки input, на который у вас есть права (или /cancel):",
            parse_mode="Markdown",
            reply_markup=kb.back_main(),
        )
        return

    if data.startswith("run:"):
        await _answer(cb, "Запускаю…")
        job_type = data.split(":", 1)[1]
        value_key = "create_file" if job_type == "episode" else "create_query"
        query = str(context.user_data.get(value_key) or "").strip()
        if not query:
            await msg.edit_text("❌ Данные запуска устарели. Введите тему или файл ещё раз.", reply_markup=kb.create_menu())
            return
        try:
            if job_type == "dorama":
                result = await _call_api(api.start_dorama, query)
            elif job_type == "licensed":
                result = await _call_api(api.start_licensed, query)
            elif job_type == "episode":
                result = await _call_api(api.start_episode, query)
            else:
                await msg.edit_text("❌ Неизвестный тип", reply_markup=kb.back_main())
                return
            job_id = result.get("id", "?")
            context.user_data.pop("create_type", None)
            await msg.edit_text(
                f"🚀 Задача запущена\n\n"
                f"🆔 {job_id}\n"
                f"📝 {result.get('title', query)}\n"
                f"📊 {_progress_bar(result.get('progress', 0))}",
                reply_markup=kb.job_actions(job_id, running=True),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка запуска: {e}", reply_markup=kb.back_main())
        return

    if data.startswith("job:log:"):
        await _answer(cb, "Загружаю…")
        job_id = data.split(":")[-1]
        try:
            dash = await _call_api(api.dashboard)
            job = next((j for j in dash.get("jobs", []) if j["id"] == job_id), None)
        except Exception:
            job = None
        if not job:
            await msg.edit_text("Задача не найдена", reply_markup=kb.back_main())
            return
        emoji = STATUS_EMOJI.get(job["status"], "❓")
        logs = job.get("logs", [])[-15:]
        if logs:
            log_text = "\n".join(f"  `{l}`" for l in logs)
        else:
            log_text = f"  _{job.get('message', 'нет данных')}_"
        running = job["status"] in ("running", "queued", "cancelling")
        lines = [
            f"{emoji} *Задача `{job_id}`*\n",
            f"📝 {job['title'][:60]}",
            f"{_progress_bar(job['progress'])} — {job.get('message', '')}\n",
            f"*Лог:*\n{log_text}",
        ]
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb.job_actions(job_id, running=running))
        return

    if data.startswith("job:cancel:"):
        await _answer(cb, "Отменяю…")
        job_id = data.split(":")[-1]
        try:
            await _call_api(api.cancel_job, job_id)
            await msg.edit_text(f"🚫 Задача `{job_id}` отменяется…", parse_mode="Markdown", reply_markup=kb.back_main())
        except Exception as e:
            err = str(e)
            if "409" in err or "уже завершен" in err.lower() or "Conflict" in err:
                await msg.edit_text(
                    f"✅ Задача `{job_id}` уже завершена",
                    parse_mode="Markdown",
                    reply_markup=kb.back_main(),
                )
            else:
                await msg.edit_text(f"❌ Ошибка отмены: {e}", reply_markup=kb.back_main())
        return

    if data == "menu:doctor":
        await _answer(cb, "Проверяю…")
        try:
            result = await _call_api(api.run_doctor)
            job_id = result.get("id", "?")
            await msg.edit_text(
                f"🔍 *Проверка системы*\n\n🆔 `{job_id}`\nЗапущена…",
                parse_mode="Markdown",
                reply_markup=kb.job_actions(job_id, running=True),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb.back_main())
        return

    if data == "menu:scheduler":
        await _answer(cb, "Проверяю…")
        try:
            result = await _call_api(api.run_scheduler)
            job_id = result.get("id", "?")
            await msg.edit_text(
                f"📅 *Проверка расписания*\n\n🆔 `{job_id}`\nВыполняется…",
                parse_mode="Markdown",
                reply_markup=kb.job_actions(job_id, running=True),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=kb.back_main())
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None or message.text is None or context.user_data is None:
        return
    text = message.text.strip()
    if text == "/cancel":
        context.user_data.clear()
        await message.reply_text("Отменено.", reply_markup=kb.main_menu())
        return

    create_type = context.user_data.get("create_type")
    if not create_type:
        return

    if create_type in ("dorama", "licensed"):
        context.user_data["create_query"] = text[:180]
        confirmation = f"📝 Тема: {context.user_data['create_query']}\n\nЗапустить?"
        markup = kb.confirm_create(create_type)

    elif create_type == "episode":
        context.user_data["create_file"] = Path(text).name[:255]
        confirmation = (
            f"🎞 Файл: {context.user_data['create_file']}\n\n"
            "Нажимая кнопку, вы подтверждаете право использовать этот материал."
        )
        markup = kb.confirm_create("episode")
    else:
        return

    try:
        await context.bot.edit_message_text(
            confirmation,
            chat_id=context.user_data["control_chat_id"],
            message_id=context.user_data["control_message_id"],
            reply_markup=markup,
        )
    except Exception:
        await message.reply_text(confirmation, reply_markup=markup)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = update.effective_message
    if message is None or context.user_data is None:
        return
    context.user_data.clear()
    await message.reply_text("↩ В главное меню", reply_markup=kb.main_menu())


def _build_application(token: str) -> Application[Any, Any, Any, Any, Any, Any]:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


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
            app = _build_application(BOT_TOKEN)
            initialized = started = polling = False
            try:
                await app.initialize()
                initialized = True
                await app.start()
                started = True
                if app.updater is None:
                    raise RuntimeError("Telegram updater недоступен")
                await app.updater.start_polling(drop_pending_updates=True)
                polling = True
                log.info("Telegram-бот запущен (user_id=%s)", ALLOWED_USER)
                while not stop_event.is_set():
                    await asyncio.sleep(0.25)
            finally:
                if polling and app.updater is not None:
                    await app.updater.stop()
                if started:
                    await app.stop()
                if initialized:
                    await app.shutdown()

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

    app = _build_application(BOT_TOKEN)
    log.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
