"""Потокобезопасная SQLite-очередь клипов и слотов публикации."""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    caption TEXT NOT NULL,
    source_video TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    platform TEXT,
    created_at TEXT NOT NULL,
    posted_at TEXT,
    claim_token TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS schedule_log (
    date TEXT NOT NULL,
    slot TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    PRIMARY KEY (date, slot)
);

CREATE TABLE IF NOT EXISTS platform_schedule_log (
    date TEXT NOT NULL,
    slot TEXT NOT NULL,
    platform TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    PRIMARY KEY (date, slot, platform)
);

CREATE INDEX IF NOT EXISTS idx_clips_status_created
    ON clips(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_clips_created
    ON clips(created_at DESC, id DESC);
"""

_CLIP_MIGRATIONS: dict[str, str] = {
    "claim_token": "TEXT",
    "claimed_by": "TEXT",
    "claimed_at": "TEXT",
    "last_error": "TEXT",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    database = path or DB_PATH
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Создать схему и безопасно довести старую базу до текущей версии."""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(clips)")}
        for name, sql_type in _CLIP_MIGRATIONS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {sql_type}")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_clips_status_created
                ON clips(status, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_clips_created
                ON clips(created_at DESC, id DESC);
            """
        )


def add_pending(file_path: str, caption: str, source_video: str) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO clips (file_path, caption, source_video, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (file_path, caption, source_video, _utc_now()),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite не вернул ID добавленного ролика")
        return int(cursor.lastrowid)


def get_oldest_pending() -> sqlite3.Row | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM clips WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        return cast(sqlite3.Row | None, row)


def get_clip(clip_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return cast(sqlite3.Row | None, row)


def claim_pending(claimed_by: str, clip_id: int | None = None) -> sqlite3.Row | None:
    """Атомарно зарезервировать один pending-клип для единственного обработчика.

    Claim намеренно не истекает автоматически: после аварии во время внешней
    загрузки безопаснее оставить запись для ручной сверки, чем опубликовать дубль.
    """
    if not claimed_by.strip():
        raise ValueError("claimed_by не может быть пустым")
    token = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if clip_id is None:
            row = conn.execute(
                "SELECT id FROM clips WHERE status = 'pending' "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM clips WHERE id = ? AND status = 'pending'", (clip_id,)
            ).fetchone()
        if row is None:
            return None
        claimed_id = int(row["id"])
        cursor = conn.execute(
            "UPDATE clips SET status = 'publishing', claim_token = ?, claimed_by = ?, "
            "claimed_at = ?, last_error = NULL WHERE id = ? AND status = 'pending'",
            (token, claimed_by, _utc_now(), claimed_id),
        )
        if cursor.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM clips WHERE id = ? AND claim_token = ?", (claimed_id, token)
        ).fetchone()
        return cast(sqlite3.Row | None, claimed)


def release_claim(clip_id: int, claim_token: str, error: str | None = None) -> bool:
    """Вернуть клип в очередь после подтверждённой ошибки до внешней публикации."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE clips SET status = 'pending', claim_token = NULL, claimed_by = NULL, "
            "claimed_at = NULL, last_error = ? "
            "WHERE id = ? AND status = 'publishing' AND claim_token = ?",
            ((error or "")[:2000] or None, clip_id, claim_token),
        )
        return cursor.rowcount == 1


def recover_interrupted_claim(clip_id: int) -> bool:
    """Ручное восстановление только после проверки, что внешний пост не появился."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE clips SET status = 'pending', claim_token = NULL, claimed_by = NULL, "
            "claimed_at = NULL, last_error = 'Восстановлено вручную после проверки' "
            "WHERE id = ? AND status = 'publishing'",
            (clip_id,),
        )
        return cursor.rowcount == 1


def mark_posted_claimed(
    clip_id: int,
    claim_token: str,
    platform: str,
    file_path: str | None = None,
) -> None:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE clips SET status = 'posted', platform = ?, posted_at = ?, "
            "file_path = COALESCE(?, file_path), claim_token = NULL, claimed_by = NULL, "
            "claimed_at = NULL, last_error = NULL "
            "WHERE id = ? AND status = 'publishing' AND claim_token = ?",
            (platform, _utc_now(), file_path, clip_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Claim ролика #{clip_id} потерян; публикация не зафиксирована")


def mark_rejected_claimed(clip_id: int, claim_token: str, file_path: str) -> None:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE clips SET status = 'rejected', file_path = ?, claim_token = NULL, "
            "claimed_by = NULL, claimed_at = NULL, last_error = NULL "
            "WHERE id = ? AND status = 'publishing' AND claim_token = ?",
            (file_path, clip_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Claim ролика #{clip_id} потерян; отклонение не зафиксировано")


def update_file_path(clip_id: int, file_path: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE clips SET file_path = ? WHERE id = ?", (file_path, clip_id))


def update_caption(clip_id: int, caption: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE clips SET caption = ? WHERE id = ? AND status = 'pending'",
            (caption, clip_id),
        )
        return cursor.rowcount == 1


def count_pending() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM clips WHERE status = 'pending'").fetchone()
        return int(row["c"])


def counts_by_status() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM clips GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}


def recent(limit: int = 20) -> list[sqlite3.Row]:
    safe_limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM clips ORDER BY created_at DESC, id DESC LIMIT ?", (safe_limit,)
        ).fetchall()


def try_claim_platform_slot(date_str: str, slot: str, platform: str) -> bool:
    """Атомарно зарезервировать слот; False означает, что его уже взял другой запуск."""
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO platform_schedule_log "
            "(date, slot, platform, fired_at) VALUES (?, ?, ?, ?)",
            (date_str, slot, platform, _utc_now()),
        )
        return cursor.rowcount == 1


def release_platform_slot(date_str: str, slot: str, platform: str) -> None:
    """Разрешить повтор после обработанной ошибки текущего запуска."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM platform_schedule_log WHERE date = ? AND slot = ? AND platform = ?",
            (date_str, slot, platform),
        )


# Совместимость со старыми CLI/интеграциями. Новая публикация использует claims.
def slot_already_fired(date_str: str, slot: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM schedule_log WHERE date = ? AND slot = ?", (date_str, slot)
        ).fetchone() is not None


def mark_slot_fired(date_str: str, slot: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_log (date, slot, fired_at) VALUES (?, ?, ?)",
            (date_str, slot, _utc_now()),
        )


def platform_slot_already_fired(date_str: str, slot: str, platform: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM platform_schedule_log WHERE date = ? AND slot = ? AND platform = ?",
            (date_str, slot, platform),
        ).fetchone() is not None


def mark_platform_slot_fired(date_str: str, slot: str, platform: str) -> None:
    try_claim_platform_slot(date_str, slot, platform)
