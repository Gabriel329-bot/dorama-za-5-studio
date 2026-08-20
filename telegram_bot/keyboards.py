"""Инлайн-клавиатуры — всё через CallbackQuery, edit_message."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.api_client import JsonDict


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус", callback_data="menu:status"),
         InlineKeyboardButton("🎬 Создать", callback_data="menu:create")],
        [InlineKeyboardButton("📋 Очередь", callback_data="menu:queue"),
         InlineKeyboardButton("⚙ Настройки", callback_data="menu:settings")],
        [InlineKeyboardButton("🔍 Проверка системы", callback_data="menu:doctor"),
         InlineKeyboardButton("📅 Расписание", callback_data="menu:scheduler")],
    ])


def back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Меню", callback_data="menu:main")]
    ])


def create_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Дорама-радар", callback_data="create:dorama")],
        [InlineKeyboardButton("🎓 Лицензия CC", callback_data="create:licensed")],
        [InlineKeyboardButton("🎞 Серия → 5 мин", callback_data="create:episode")],
        [InlineKeyboardButton("← Меню", callback_data="menu:main")],
    ])


def confirm_create(job_type: str) -> InlineKeyboardMarkup:
    label = "✅ Подтверждаю права и запускаю" if job_type == "episode" else "▶ Запустить"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"run:{job_type}")],
        [InlineKeyboardButton("← Назад", callback_data="menu:create")],
    ])


def job_actions(job_id: str, running: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if running:
        buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"job:log:{job_id}")])
    buttons.append([InlineKeyboardButton("📜 Лог", callback_data=f"job:log:{job_id}")])
    if running:
        buttons.append([InlineKeyboardButton("✖ Отмена", callback_data=f"job:cancel:{job_id}")])
    buttons.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def clip_actions(clip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ YouTube", callback_data=f"clip:yt:{clip_id}")],
        [InlineKeyboardButton("🗑 Отклонить", callback_data=f"clip:reject:{clip_id}")],
        [InlineKeyboardButton("← Меню", callback_data="menu:main")],
    ])


def queue_list(clips: list[JsonDict], page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    total = len(clips)
    start = page * per_page
    end = min(start + per_page, total)
    buttons: list[list[InlineKeyboardButton]] = []
    for clip in clips[start:end]:
        cap = clip["caption"][:30] + ("…" if len(clip["caption"]) > 30 else "")
        buttons.append([InlineKeyboardButton(
            f"#{clip['id']} {cap}", callback_data=f"clip:detail:{clip['id']}"
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"queue:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{start + 1}–{end}/{total}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"queue:page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def settings_view() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu:settings")],
        [InlineKeyboardButton("← Меню", callback_data="menu:main")],
    ])
