"""SQLite-очередь клипов: что нарезано, что уже опубликовано."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    caption TEXT NOT NULL,
    source_video TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | posted
    platform TEXT,
    created_at TEXT NOT NULL,
    posted_at TEXT
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
"""


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def add_pending(file_path: str, caption: str, source_video: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO clips (file_path, caption, source_video, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (file_path, caption, source_video, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_oldest_pending() -> sqlite3.Row | None:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM clips WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        return cur.fetchone()


def get_clip(clip_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()


def mark_posted(clip_id: int, platform: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE clips SET status = 'posted', platform = ?, posted_at = ? WHERE id = ?",
            (platform, datetime.now(timezone.utc).isoformat(), clip_id),
        )


def update_file_path(clip_id: int, file_path: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE clips SET file_path = ? WHERE id = ?",
            (file_path, clip_id),
        )


def update_caption(clip_id: int, caption: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE clips SET caption = ? WHERE id = ?",
            (caption, clip_id),
        )


def mark_rejected(clip_id: int, file_path: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE clips SET status = 'rejected', file_path = ? WHERE id = ? AND status = 'pending'",
            (file_path, clip_id),
        )


def count_pending() -> int:
    with _connect() as conn:
        cur = conn.execute("SELECT COUNT(*) AS c FROM clips WHERE status = 'pending'")
        return cur.fetchone()["c"]


def counts_by_status() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM clips GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}


def recent(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as conn:
        cur = conn.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,))
        return cur.fetchall()


def slot_already_fired(date_str: str, slot: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM schedule_log WHERE date = ? AND slot = ?", (date_str, slot)
        )
        return cur.fetchone() is not None


def mark_slot_fired(date_str: str, slot: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_log (date, slot, fired_at) VALUES (?, ?, ?)",
            (date_str, slot, datetime.now(timezone.utc).isoformat()),
        )


def platform_slot_already_fired(date_str: str, slot: str, platform: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM platform_schedule_log WHERE date = ? AND slot = ? AND platform = ?",
            (date_str, slot, platform),
        ).fetchone()
        return row is not None


def mark_platform_slot_fired(date_str: str, slot: str, platform: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO platform_schedule_log (date, slot, platform, fired_at) VALUES (?, ?, ?, ?)",
            (date_str, slot, platform, datetime.now(timezone.utc).isoformat()),
        )
