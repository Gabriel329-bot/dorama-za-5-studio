from __future__ import annotations

import io
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from dorama.licensed_sources import _validate_download_url
from job_control import cancellable_wait
from storage import db
from telegram_bot import api_client
from webapp.app import app
from webapp.health import HealthService
from webapp.job_store import JobStore
from webapp.jobs import JobManager
from webapp.schemas import DoramaRequest
from webapp.uploads import UploadTooLargeError, save_video_upload


class ApiSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_mutation_requires_csrf_token(self) -> None:
        denied = self.client.post("/api/jobs/not-found/cancel")
        self.assertEqual(403, denied.status_code)
        session = self.client.get("/api/session")
        self.assertEqual(200, session.status_code)
        token = session.json()["csrf_token"]
        allowed = self.client.post(
            "/api/jobs/not-found/cancel", headers={"X-Dorama-CSRF": token}
        )
        self.assertEqual(404, allowed.status_code)

    def test_untrusted_host_is_rejected(self) -> None:
        response = self.client.get("/api/session", headers={"host": "attacker.invalid"})
        self.assertEqual(400, response.status_code)

    def test_request_models_are_strict_and_forbid_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            DoramaRequest(query="дорамы", limit="10")
        with self.assertRaises(ValidationError):
            DoramaRequest(query="дорамы", limit=10, unexpected=True)

    def test_download_allowlist_rejects_ssrf_url(self) -> None:
        with self.assertRaises(RuntimeError):
            _validate_download_url("http://127.0.0.1/private.mp4", "Wikimedia Commons")
        self.assertEqual(
            "https://upload.wikimedia.org/video.mp4",
            _validate_download_url(
                "https://upload.wikimedia.org/video.mp4", "Wikimedia Commons"
            ),
        )

    def test_telegram_local_client_ignores_proxy_environment(self) -> None:
        with patch("telegram_bot.api_client.httpx.Client") as client_factory:
            api_client._client()
        client_factory.assert_called_once()
        kwargs = client_factory.call_args.kwargs
        self.assertEqual(api_client.BASE, kwargs["base_url"])
        self.assertFalse(kwargs["trust_env"])


class AtomicQueueTests(unittest.TestCase):
    def test_only_one_worker_can_claim_a_clip_and_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "queue.db"
            with patch("storage.db.DB_PATH", database):
                db.init_db()
                clip_id = db.add_pending("video.mp4", "caption", "source")
                with ThreadPoolExecutor(max_workers=8) as executor:
                    claims = list(executor.map(lambda _: db.claim_pending("worker"), range(8)))
                claimed = [row for row in claims if row is not None]
                self.assertEqual(1, len(claimed))
                self.assertEqual(clip_id, claimed[0]["id"])

                with ThreadPoolExecutor(max_workers=8) as executor:
                    slots = list(
                        executor.map(
                            lambda _: db.try_claim_platform_slot(
                                "2026-08-20", "12:00", "youtube"
                            ),
                            range(8),
                        )
                    )
                self.assertEqual(1, sum(slots))

    def test_queue_indexes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "queue.db"
            with patch("storage.db.DB_PATH", database):
                db.init_db()
                with db._connect() as connection:
                    indexes = {
                        row["name"]
                        for row in connection.execute("PRAGMA index_list(clips)").fetchall()
                    }
            self.assertIn("idx_clips_status_created", indexes)
            self.assertIn("idx_clips_created", indexes)
            self.assertIn("idx_clips_origin_job", indexes)

    def test_origin_job_prevents_duplicate_queue_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "queue.db"
            with patch("storage.db.DB_PATH", database):
                db.init_db()
                first = db.add_pending(
                    "first.mp4",
                    "caption",
                    "source",
                    origin_job_id="job-1",
                )
                second = db.add_pending(
                    "second.mp4",
                    "caption",
                    "source",
                    origin_job_id="job-1",
                )
                row = db.get_clip_by_origin_job("job-1")
            self.assertEqual(first, second)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual("first.mp4", row["file_path"])


class ResourceBoundTests(unittest.TestCase):
    def test_upload_is_streamed_limited_and_partial_file_is_deleted(self) -> None:
        payload = b"\x00\x00\x00\x18ftypisom" + b"x" * 64
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path, size = save_video_upload(
                io.BytesIO(payload), "episode.mp4", directory, max_bytes=1024
            )
            self.assertEqual(len(payload), size)
            self.assertTrue(path.is_file())
            with self.assertRaises(UploadTooLargeError):
                save_video_upload(
                    io.BytesIO(payload), "too-large.mp4", directory, max_bytes=16
                )
            self.assertEqual(1, len(list(directory.iterdir())))

    def test_job_history_is_bounded(self) -> None:
        manager = JobManager(max_workers=1, max_history=10, max_log_lines=5)
        for index in range(18):
            manager.submit("test", str(index), lambda: "ok")
        manager.executor.shutdown(wait=True)
        self.assertLessEqual(len(manager.jobs), 10)

    def test_health_probes_are_cached(self) -> None:
        service = HealthService(ttl_seconds=60)
        with (
            patch.object(service, "_ollama_ready", return_value=True) as ollama,
            patch.object(service, "_scheduler_enabled", return_value=True) as scheduler,
        ):
            self.assertEqual(service.snapshot("http://localhost", "model"), service.snapshot("http://localhost", "model"))
        ollama.assert_called_once()
        scheduler.assert_called_once()


class RestartRecoveryTests(unittest.TestCase):
    @staticmethod
    def _stored_record(status: str = "running") -> dict[str, object]:
        return {
            "id": "resume-job-1",
            "kind": "dorama",
            "title": "Восстановление",
            "status": status,
            "progress": 52,
            "message": "Старый процесс",
            "logs": ["этап сохранён"],
            "created_at": "2026-08-20T10:00:00+03:00",
            "finished_at": None,
            "result": None,
            "resume_count": 0,
        }

    def test_interrupted_resumable_job_is_enqueued_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            store.initialize()
            store.save(
                self._stored_record(),
                {"query": "дорама", "limit": 10},
                resumable=True,
            )
            called = Event()

            def handler(job_id: str, kind: str, payload: dict[str, object]) -> str:
                self.assertEqual("resume-job-1", job_id)
                self.assertEqual("dorama", kind)
                self.assertEqual("дорама", payload["query"])
                called.set()
                return "готово"

            manager = JobManager(
                store=store,
                persistent_handler=handler,
            )
            self.assertEqual(1, manager.start())
            self.assertTrue(called.wait(2))
            for _ in range(40):
                job = manager.recent(1)[0]
                if job["status"] == "succeeded":
                    break
                time.sleep(0.025)
            self.assertEqual("succeeded", job["status"])
            self.assertEqual(1, job["resume_count"])
            manager.shutdown()

    def test_recovery_loads_all_active_jobs_beyond_history_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            store.initialize()
            for index in range(4):
                record = self._stored_record(
                    status="running" if index < 3 else "succeeded"
                )
                record["id"] = f"job-{index}"
                store.save(record, {"index": index}, resumable=True)
            loaded = store.load_for_recovery(history_limit=1)
            active = [
                item
                for item in loaded
                if item.record["status"] == "running"
            ]
            self.assertEqual(3, len(active))
            self.assertEqual(4, len(loaded))

    def test_external_publication_is_not_replayed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            store.initialize()
            record = self._stored_record()
            record["kind"] = "youtube"
            store.save(
                record,
                {"clip_id": 7, "privacy": "private"},
                resumable=False,
            )
            called = Event()
            manager = JobManager(
                store=store,
                persistent_handler=lambda *_args: called.set(),
            )
            self.assertEqual(0, manager.start())
            job = manager.recent(1)[0]
            self.assertEqual("attention", job["status"])
            self.assertFalse(called.is_set())
            manager.shutdown()

    def test_queued_publication_can_resume_if_it_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            store.initialize()
            record = self._stored_record(status="queued")
            record["kind"] = "youtube"
            store.save(record, {"clip_id": 7}, resumable=False)
            called = Event()
            manager = JobManager(
                store=store,
                persistent_handler=lambda *_args: called.set(),
            )
            self.assertEqual(1, manager.start())
            self.assertTrue(called.wait(2))
            manager.shutdown()

    def test_graceful_shutdown_marks_running_job_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            started = Event()

            def handler(_job_id: str, _kind: str, _payload: dict[str, object]) -> None:
                started.set()
                while True:
                    cancellable_wait(0.05)

            manager = JobManager(
                store=store,
                persistent_handler=handler,
            )
            manager.start()
            manager.submit_persistent(
                "dorama",
                "Долгая задача",
                {"query": "дорама"},
            )
            self.assertTrue(started.wait(2))
            manager.shutdown()
            for _ in range(40):
                stored = store.load_recent(1)[0]
                if stored.record["status"] == "paused":
                    break
                time.sleep(0.025)
            self.assertEqual("paused", stored.record["status"])


if __name__ == "__main__":
    unittest.main()
