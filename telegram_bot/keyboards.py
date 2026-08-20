"""Inline-клавиатуры полного Telegram-интерфейса студии."""
from __future__ import annotations

import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.api_client import JsonDict


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Статус", callback_data="menu:status"),
                InlineKeyboardButton("🧭 Процессы", callback_data="menu:jobs"),
            ],
            [
                InlineKeyboardButton("🎬 Создать", callback_data="menu:create"),
                InlineKeyboardButton("📋 Публикации", callback_data="menu:queue"),
            ],
            [
                InlineKeyboardButton("⚙ Пайплайн", callback_data="menu:settings"),
                InlineKeyboardButton("▶ YouTube", callback_data="menu:youtube"),
            ],
            [
                InlineKeyboardButton("🔍 Диагностика", callback_data="menu:doctor"),
                InlineKeyboardButton("📅 Проверить слоты", callback_data="menu:scheduler"),
            ],
        ]
    )


def back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Главное меню", callback_data="menu:main")]]
    )


def create_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📡 Дорама-радар", callback_data="create:dorama")],
            [InlineKeyboardButton("🎓 Автопоиск CC", callback_data="create:licensed")],
            [
                InlineKeyboardButton("🎞 Пересказ файла", callback_data="create:upload:recap"),
                InlineKeyboardButton("🌐 Перевод файла", callback_data="create:upload:translate"),
            ],
            [
                InlineKeyboardButton("📁 Из input: пересказ", callback_data="create:path:recap"),
                InlineKeyboardButton("📁 Из input: перевод", callback_data="create:path:translate"),
            ],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )


def create_options(job_type: str, limit: int = 10) -> InlineKeyboardMarkup:
    limits = [6, 10, 15, 25, 40]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✓ ' if value == limit else ''}{value}",
                    callback_data=f"create:limit:{job_type}:{value}",
                )
                for value in limits
            ],
            [InlineKeyboardButton("▶ Запустить", callback_data=f"run:{job_type}")],
            [InlineKeyboardButton("← Создание", callback_data="menu:create")],
        ]
    )


def confirm_create(job_type: str) -> InlineKeyboardMarkup:
    """Совместимость с прежним подтверждением запуска."""
    label = "✅ Подтверждаю права и запускаю" if job_type == "episode" else "▶ Запустить"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"run:{job_type}")],
            [InlineKeyboardButton("← Создание", callback_data="menu:create")],
        ]
    )


def episode_focus() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Без дополнительного фокуса", callback_data="episode:focus:skip")],
            [InlineKeyboardButton("← Создание", callback_data="menu:create")],
        ]
    )


def confirm_episode(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтверждаю права и запускаю", callback_data=f"episode:run:{mode}")],
            [InlineKeyboardButton("← Создание", callback_data="menu:create")],
        ]
    )


def translation_interval(duration: float) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if duration <= 300.0:
        rows.append(
            [InlineKeyboardButton("Перевести весь файл", callback_data="episode:interval:full")]
        )
    else:
        starts = sorted({0, max(0, int(duration // 2 - 150)), max(0, int(duration - 300))})
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        f"{_clock(start)}–{_clock(min(duration, start + 300))}",
                        callback_data=f"episode:interval:{start}:{int(min(duration, start + 300))}",
                    )
                ]
                for start in starts
            ]
        )
    rows.append([InlineKeyboardButton("← Создание", callback_data="menu:create")])
    return InlineKeyboardMarkup(rows)


def _clock(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def queue_filters(selected: str = "all") -> InlineKeyboardMarkup:
    labels = {
        "all": "Все",
        "pending": "Проверка",
        "posted": "Готово",
        "rejected": "Отклонено",
    }
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✓ ' if key == selected else ''}{label}",
                    callback_data=f"queue:filter:{key}",
                )
                for key, label in labels.items()
            ],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )


def queue_list(
    clips: list[JsonDict],
    page: int = 0,
    per_page: int = 5,
    *,
    filter_status: str = "all",
) -> InlineKeyboardMarkup:
    total = len(clips)
    pages = max(1, math.ceil(total / per_page))
    safe_page = max(0, min(page, pages - 1))
    start = safe_page * per_page
    end = min(start + per_page, total)
    buttons: list[list[InlineKeyboardButton]] = []
    status_icon = {"pending": "🟡", "publishing": "🔄", "posted": "🟢", "rejected": "⚫"}
    for clip in clips[start:end]:
        caption = str(clip.get("caption") or "Без названия").splitlines()[0]
        label = caption[:28] + ("…" if len(caption) > 28 else "")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{status_icon.get(str(clip.get('status')), '⚪')} #{clip['id']} {label}",
                    callback_data=f"clip:detail:{clip['id']}",
                )
            ]
        )
    if total:
        nav: list[InlineKeyboardButton] = []
        if safe_page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"queue:page:{safe_page - 1}"))
        nav.append(InlineKeyboardButton(f"{start + 1}–{end}/{total}", callback_data="noop"))
        if end < total:
            nav.append(InlineKeyboardButton("▶", callback_data=f"queue:page:{safe_page + 1}"))
        buttons.append(nav)
    buttons.extend(
        [
            [InlineKeyboardButton("Фильтры", callback_data=f"queue:filters:{filter_status}")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu:queue")],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(buttons)


def clip_actions(clip: JsonDict | int) -> InlineKeyboardMarkup:
    item: JsonDict = {"id": clip, "status": "pending", "file_exists": True} if isinstance(clip, int) else clip
    clip_id = int(item["id"])
    status = str(item.get("status") or "")
    rows: list[list[InlineKeyboardButton]] = []
    if item.get("file_exists"):
        rows.append([InlineKeyboardButton("▶ Предпросмотр", callback_data=f"clip:preview:{clip_id}")])
    if status == "pending":
        rows.extend(
            [
                [InlineKeyboardButton("✏ Текст и хештеги", callback_data=f"clip:edit:{clip_id}")],
                [InlineKeyboardButton("▶ Отправить на YouTube", callback_data=f"clip:publish:{clip_id}")],
                [InlineKeyboardButton("↘ Отклонить", callback_data=f"clip:rejectconfirm:{clip_id}")],
                [InlineKeyboardButton("🗑 Удалить полностью", callback_data=f"clip:deleteconfirm:{clip_id}")],
            ]
        )
    elif status != "publishing":
        rows.append([InlineKeyboardButton("🗑 Удалить полностью", callback_data=f"clip:deleteconfirm:{clip_id}")])
    rows.extend(
        [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"clip:detail:{clip_id}")],
            [InlineKeyboardButton("← Очередь", callback_data="menu:queue")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def publish_privacy(clip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔒 Приватный", callback_data=f"clip:privacy:{clip_id}:private")],
            [InlineKeyboardButton("🔗 По ссылке", callback_data=f"clip:privacy:{clip_id}:unlisted")],
            [InlineKeyboardButton("🌍 Публичный", callback_data=f"clip:publicconfirm:{clip_id}")],
            [InlineKeyboardButton("← Ролик", callback_data=f"clip:detail:{clip_id}")],
        ]
    )


def confirm_action(action: str, item_id: int | str, back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, выполнить", callback_data=f"{action}:{item_id}")],
            [InlineKeyboardButton("Нет, назад", callback_data=back_callback)],
        ]
    )


def jobs_list(jobs: list[JsonDict], filter_name: str = "all", page: int = 0) -> InlineKeyboardMarkup:
    per_page = 5
    total = len(jobs)
    pages = max(1, math.ceil(total / per_page))
    safe_page = max(0, min(page, pages - 1))
    start = safe_page * per_page
    end = min(start + per_page, total)
    rows: list[list[InlineKeyboardButton]] = []
    icons = {"running": "🔄", "queued": "⏳", "paused": "⏸", "attention": "⚠", "succeeded": "✅", "failed": "❌", "cancelled": "🚫", "cancelling": "⏹"}
    for job in jobs[start:end]:
        title = str(job.get("title") or "Задача")[:30]
        rows.append(
            [InlineKeyboardButton(f"{icons.get(str(job.get('status')), '•')} {title}", callback_data=f"job:log:{job['id']}")]
        )
    nav: list[InlineKeyboardButton] = []
    if safe_page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"jobs:page:{safe_page - 1}"))
    if total:
        nav.append(InlineKeyboardButton(f"{start + 1}–{end}/{total}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"jobs:page:{safe_page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend(
        [
            [
                InlineKeyboardButton(f"{'✓ ' if filter_name == 'all' else ''}Все", callback_data="jobs:filter:all"),
                InlineKeyboardButton(f"{'✓ ' if filter_name == 'active' else ''}Активные", callback_data="jobs:filter:active"),
                InlineKeyboardButton(f"{'✓ ' if filter_name == 'errors' else ''}Ошибки", callback_data="jobs:filter:errors"),
            ],
            [InlineKeyboardButton("🧹 Очистить завершённые", callback_data="jobs:clearconfirm")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu:jobs")],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def job_actions(job_id: str, running: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Обновить", callback_data=f"job:log:{job_id}")]]
    if running:
        rows.append([InlineKeyboardButton("✖ Отменить", callback_data=f"job:cancelconfirm:{job_id}")])
    rows.extend(
        [
            [InlineKeyboardButton("← Процессы", callback_data="menu:jobs")],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def settings_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎙 Whisper", callback_data="settings:whisper"),
                InlineKeyboardButton("🤖 Ollama", callback_data="settings:ollama"),
            ],
            [
                InlineKeyboardButton("🗣 Голос", callback_data="settings:voice"),
                InlineKeyboardButton("🎛 Монтаж", callback_data="settings:pipeline"),
            ],
            [InlineKeyboardButton("📡 Источники", callback_data="settings:sources")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu:settings")],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )


def settings_view() -> InlineKeyboardMarkup:
    """Совместимость со старой функцией клавиатуры."""
    return settings_main()


def whisper_settings(current_model: str, device: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{'✓ ' if model == current_model else ''}{model}", callback_data=f"pset:model:{model}")
                for model in ("tiny", "base", "small")
            ],
            [
                InlineKeyboardButton(f"{'✓ ' if model == current_model else ''}{model}", callback_data=f"pset:model:{model}")
                for model in ("medium", "large-v3")
            ],
            [
                InlineKeyboardButton(f"{'✓ ' if value == device else ''}{value.upper()}", callback_data=f"pset:device:{value}")
                for value in ("cuda", "cpu")
            ],
            [InlineKeyboardButton("← Настройки", callback_data="menu:settings")],
        ]
    )


def voice_settings(voice: str, rate: str, pitch: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{'✓ ' if 'Dmitry' in voice else ''}Дмитрий", callback_data="pset:voice:dmitry"), InlineKeyboardButton(f"{'✓ ' if 'Svetlana' in voice else ''}Светлана", callback_data="pset:voice:svetlana")],
            [
                InlineKeyboardButton(f"{'✓ ' if value == rate else ''}{value}", callback_data=f"pset:rate:{token}")
                for value, token in (("+0%", "p0"), ("+8%", "p8"), ("+12%", "p12"), ("+18%", "p18"))
            ],
            [
                InlineKeyboardButton(f"{'✓ ' if value == pitch else ''}{value}", callback_data=f"pset:pitch:{token}")
                for value, token in (("-4Hz", "m4"), ("-2Hz", "m2"), ("+0Hz", "p0"), ("+2Hz", "p2"))
            ],
            [InlineKeyboardButton("← Настройки", callback_data="menu:settings")],
        ]
    )


def pipeline_settings(settings: JsonDict) -> InlineKeyboardMarkup:
    duration = int(settings["target_duration_seconds"])
    scenes = int(settings["scene_count"])
    words = int(settings["target_script_words"])
    volume = float(settings["original_audio_volume"])
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{'✓ ' if value == duration else ''}{value // 60} мин", callback_data=f"pset:duration:{value}") for value in (60, 180, 300, 420, 600)],
            [InlineKeyboardButton(f"{'✓ ' if value == scenes else ''}{value} сцен", callback_data=f"pset:scenes:{value}") for value in (6, 9, 12, 15)],
            [InlineKeyboardButton(f"{'✓ ' if value == words else ''}{value} слов", callback_data=f"pset:words:{value}") for value in (400, 680, 900, 1200)],
            [InlineKeyboardButton(f"{'✓ ' if abs(value - volume) < 0.001 else ''}{int(value * 100)}% фон", callback_data=f"pset:volume:{int(value * 100):02d}") for value in (0.0, 0.08, 0.16, 0.25)],
            [InlineKeyboardButton(f"Ручная проверка: {'✅' if settings['require_review'] else '❌'}", callback_data="pset:review:toggle")],
            [InlineKeyboardButton("← Настройки", callback_data="menu:settings")],
        ]
    )


def source_settings(selected: list[str]) -> InlineKeyboardMarkup:
    labels = {
        "youtube": "YouTube",
        "bilibili": "Bilibili",
        "dailymotion": "Dailymotion",
        "internet_archive": "Archive",
        "wikimedia_commons": "Wikimedia",
    }
    rows = [
        [InlineKeyboardButton(f"{'✅' if key in selected else '⬜'} {label}", callback_data=f"pset:source:{key}")]
        for key, label in labels.items()
    ]
    rows.append([InlineKeyboardButton("← Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


def youtube_settings(enabled: bool, privacy: str) -> InlineKeyboardMarkup:
    labels = {"private": "Приватно", "unlisted": "По ссылке", "public": "Публично"}
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Расписание: {'✅' if enabled else '❌'}", callback_data="yt:toggle")],
            [InlineKeyboardButton(f"{'✓ ' if value == privacy else ''}{label}", callback_data=("yt:publicconfirm" if value == "public" else f"yt:privacy:{value}")) for value, label in labels.items()],
            [InlineKeyboardButton("🕐 Изменить время", callback_data="yt:times")],
            [InlineKeyboardButton("▶ Проверить сейчас", callback_data="menu:scheduler")],
            [InlineKeyboardButton("← Меню", callback_data="menu:main")],
        ]
    )
