from __future__ import annotations

import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from dorama.licensed_sources import _validate_download_url
from storage import db
from webapp.app import app
from webapp.health import HealthService
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


if __name__ == "__main__":
    unittest.main()
