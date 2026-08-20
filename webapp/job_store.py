"""Durable SQLite storage for restart-safe studio jobs."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias, cast

from settings import DB_PATH

JsonDict: TypeAlias = dict[str, Any]

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    resumable INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    logs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_studio_jobs_created
    ON studio_jobs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_studio_jobs_status
    ON studio_jobs(status, created_at, id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class StoredJob:
    record: JsonDict
    payload: JsonDict
    resumable: bool


class JobStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def save(
        self,
        record: Mapping[str, object],
        payload: Mapping[str, object],
        *,
        resumable: bool,
    ) -> None:
        logs = record.get("logs")
        safe_logs = list(logs) if isinstance(logs, list) else []
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO studio_jobs (
                    id, kind, title, payload_json, resumable, status, progress,
                    message, logs_json, created_at, updated_at, finished_at,
                    result, resume_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    payload_json = excluded.payload_json,
                    resumable = excluded.resumable,
                    status = excluded.status,
                    progress = excluded.progress,
                    message = excluded.message,
                    logs_json = excluded.logs_json,
                    updated_at = excluded.updated_at,
                    finished_at = excluded.finished_at,
                    result = excluded.result,
                    resume_count = excluded.resume_count
                """,
                (
                    str(record["id"]),
                    str(record["kind"]),
                    str(record["title"]),
                    json.dumps(dict(payload), ensure_ascii=False),
                    int(resumable),
                    str(record["status"]),
                    int(str(record["progress"])),
                    str(record["message"]),
                    json.dumps(safe_logs, ensure_ascii=False),
                    str(record["created_at"]),
                    _now_iso(),
                    record.get("finished_at"),
                    record.get("result"),
                    int(str(record.get("resume_count") or 0)),
                ),
            )

    def load_recent(self, limit: int) -> list[StoredJob]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM studio_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return self._decode_rows(rows)

    def load_for_recovery(self, history_limit: int) -> list[StoredJob]:
        """Load every active job plus a bounded amount of terminal history."""
        safe_limit = max(1, min(int(history_limit), 1000))
        active_statuses = ("queued", "running", "cancelling", "paused")
        placeholders = ",".join("?" for _ in active_statuses)
        with self._connect() as connection:
            active = connection.execute(
                f"SELECT * FROM studio_jobs WHERE status IN ({placeholders})",
                active_statuses,
            ).fetchall()
            history = connection.execute(
                f"SELECT * FROM studio_jobs WHERE status NOT IN ({placeholders}) "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (*active_statuses, safe_limit),
            ).fetchall()
        rows_by_id = {
            str(row["id"]): row
            for row in [*active, *history]
        }
        rows = sorted(
            rows_by_id.values(),
            key=lambda row: (str(row["created_at"]), str(row["id"])),
            reverse=True,
        )
        return self._decode_rows(rows)

    @staticmethod
    def _decode_rows(rows: list[sqlite3.Row]) -> list[StoredJob]:
        result: list[StoredJob] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                logs = json.loads(str(row["logs_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload, logs = {}, ["Повреждённая запись задачи"]
            if not isinstance(payload, dict):
                payload = {}
            if not isinstance(logs, list):
                logs = []
            record: JsonDict = {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "progress": int(row["progress"]),
                "message": str(row["message"]),
                "logs": [str(item) for item in logs],
                "created_at": str(row["created_at"]),
                "finished_at": cast(str | None, row["finished_at"]),
                "result": cast(str | None, row["result"]),
                "resume_count": int(row["resume_count"]),
            }
            result.append(
                StoredJob(
                    record=record,
                    payload=cast(JsonDict, payload),
                    resumable=bool(row["resumable"]),
                )
            )
        return result

    def delete(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM studio_jobs WHERE id = ?",
                ((job_id,) for job_id in job_ids),
            )
