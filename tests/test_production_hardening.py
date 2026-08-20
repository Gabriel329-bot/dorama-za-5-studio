from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from background_services import SchedulerService
from dorama.licensed_sources import _validate_download_url
from dorama.literal_translation import validate_translation_interval
from job_control import cancellable_wait
from runtime_support import python_module_command, resolve_app_home
from storage import db
from telegram_bot import api_client
from webapp.app import app, create_episode, job_manager
from webapp.health import HealthService
from webapp.job_handlers import execute_persistent_job
from webapp.job_store import JobStore
from webapp.jobs import JobManager
from webapp.schemas import DoramaRequest, EpisodeRequest
from webapp.uploads import UploadTooLargeError, probe_video_duration, save_video_upload


class ApiSecurityTests(unittest.TestCase):
    client: ClassVar[TestClient]

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

    def test_clear_history_endpoint_keeps_api_protected(self) -> None:
        denied = self.client.delete("/api/jobs/history")
        self.assertEqual(403, denied.status_code)
        token = self.client.get("/api/session").json()["csrf_token"]
        with patch.object(
            job_manager,
            "clear_terminal_history",
            return_value=3,
        ) as clear:
            response = self.client.delete(
                "/api/jobs/history",
                headers={"X-Dorama-CSRF": token},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"cleared": 3}, response.json())
        clear.assert_called_once_with()

    def test_queue_clip_delete_removes_record_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "queue.db"
            pending = root / "output" / "pending"
            posted = root / "output" / "posted"
            rejected = root / "output" / "rejected"
            pending.mkdir(parents=True)
            video = pending / "episode.mp4"
            subtitle = video.with_suffix(".ass")
            video.write_bytes(b"video")
            subtitle.write_text("subtitles", encoding="utf-8")
            with (
                patch("storage.db.DB_PATH", database),
                patch("webapp.app.PENDING_DIR", pending),
                patch("webapp.app.POSTED_DIR", posted),
                patch("webapp.app.REJECTED_DIR", rejected),
            ):
                db.init_db()
                clip_id = db.add_pending(str(video), "caption", "source")
                token = self.client.get("/api/session").json()["csrf_token"]
                response = self.client.delete(
                    f"/api/clips/{clip_id}",
                    headers={"X-Dorama-CSRF": token},
                )
                row = db.get_clip(clip_id)

            self.assertEqual(200, response.status_code)
            self.assertEqual(2, response.json()["deleted_files"])
            self.assertFalse(response.json()["cleanup_pending"])
            self.assertIsNone(row)
            self.assertFalse(video.exists())
            self.assertFalse(subtitle.exists())

    def test_queue_clip_delete_rejects_file_outside_media_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "queue.db"
            pending = root / "output" / "pending"
            posted = root / "output" / "posted"
            rejected = root / "output" / "rejected"
            outside = root / "do-not-delete.mp4"
            outside.write_bytes(b"private")
            with (
                patch("storage.db.DB_PATH", database),
                patch("webapp.app.PENDING_DIR", pending),
                patch("webapp.app.POSTED_DIR", posted),
                patch("webapp.app.REJECTED_DIR", rejected),
            ):
                db.init_db()
                clip_id = db.add_pending(str(outside), "caption", "source")
                token = self.client.get("/api/session").json()["csrf_token"]
                response = self.client.delete(
                    f"/api/clips/{clip_id}",
                    headers={"X-Dorama-CSRF": token},
                )
                row = db.get_clip(clip_id)

            self.assertEqual(409, response.status_code)
            self.assertTrue(outside.is_file())
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual("pending", row["status"])

    def test_untrusted_host_is_rejected(self) -> None:
        response = self.client.get("/api/session", headers={"host": "attacker.invalid"})
        self.assertEqual(400, response.status_code)

    def test_request_models_are_strict_and_forbid_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            DoramaRequest(query="дорамы", limit="10")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            DoramaRequest(query="дорамы", limit=10, unexpected=True)  # type: ignore[call-arg]

    def test_literal_translation_interval_is_limited_to_five_minutes(self) -> None:
        payload = EpisodeRequest(
            filename="episode.mp4",
            rights_confirmed=True,
            mode="translate",
            start_seconds=12.5,
            end_seconds=312.5,
        )
        self.assertEqual("translate", payload.mode)
        with self.assertRaises(ValidationError):
            EpisodeRequest(
                filename="episode.mp4",
                rights_confirmed=True,
                mode="translate",
                start_seconds=0.0,
                end_seconds=301.0,
            )

    def test_literal_translation_interval_checks_media_bounds(self) -> None:
        self.assertEqual((20.0, 320.0), validate_translation_interval(20, 320, 900))
        with self.assertRaisesRegex(ValueError, "длительность видео"):
            validate_translation_interval(20, 321, 300)

    def test_episode_endpoint_submits_selected_literal_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_dir = Path(temporary)
            source = input_dir / "episode.mp4"
            source.write_bytes(b"video")
            payload = EpisodeRequest(
                filename=source.name,
                rights_confirmed=True,
                mode="translate",
                start_seconds=120.0,
                end_seconds=320.0,
            )
            marker = object()
            with (
                patch("webapp.app.INPUT_DIR", input_dir),
                patch("webapp.app.probe_video_duration", return_value=1000.0),
                patch("webapp.app._submit_persistent_job", return_value=marker) as submit,
            ):
                result = create_episode(payload)
        self.assertIs(marker, result)
        self.assertEqual("literal-translation", submit.call_args.args[0])
        self.assertEqual(120.0, submit.call_args.args[2]["start_seconds"])
        self.assertEqual(320.0, submit.call_args.args[2]["end_seconds"])

    def test_long_translation_requires_editor_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_dir = Path(temporary)
            source = input_dir / "episode.mp4"
            source.write_bytes(b"video")
            payload = EpisodeRequest(
                filename=source.name,
                rights_confirmed=True,
                mode="translate",
            )
            with (
                patch("webapp.app.INPUT_DIR", input_dir),
                patch("webapp.app.probe_video_duration", return_value=301.0),
                self.assertRaises(HTTPException) as context,
            ):
                create_episode(payload)
        self.assertEqual(422, context.exception.status_code)
        self.assertIn("редакторе", str(context.exception.detail))

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

    def test_deletion_claim_blocks_publish_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "queue.db"
            with patch("storage.db.DB_PATH", database):
                db.init_db()
                clip_id = db.add_pending("video.mp4", "caption", "source")
                deletion_claim = db.claim_for_deletion(clip_id)
                publish_claim = db.claim_pending("youtube", clip_id)
                self.assertIsNotNone(deletion_claim)
                self.assertIsNone(publish_claim)
                assert deletion_claim is not None
                row, previous_status = deletion_claim
                self.assertEqual("pending", previous_status)
                db.delete_claimed(clip_id, str(row["claim_token"]))
                self.assertIsNone(db.get_clip(clip_id))


class ResourceBoundTests(unittest.TestCase):
    @patch("webapp.uploads.run_process")
    def test_upload_duration_probe_reads_ffmpeg_metadata(self, run_process) -> None:
        run_process.return_value = SimpleNamespace(
            stderr="Duration: 00:42:03.75, start: 0.000000, bitrate: 1000 kb/s"
        )
        self.assertEqual(2523.75, probe_video_duration(Path("episode.mp4")))

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

    def test_clear_history_preserves_active_job(self) -> None:
        manager = JobManager(max_workers=1)
        finished = manager.submit("test", "Готовая", lambda: "ok")
        for _ in range(80):
            if manager.recent(1)[0]["status"] == "succeeded":
                break
            time.sleep(0.025)

        started = Event()

        def keep_running() -> None:
            started.set()
            while True:
                cancellable_wait(0.02)

        active = manager.submit("test", "Активная", keep_running)
        self.assertTrue(started.wait(2))

        self.assertEqual(1, manager.clear_terminal_history())
        self.assertNotIn(finished["id"], manager.jobs)
        self.assertIn(active["id"], manager.jobs)

        manager.cancel(active["id"])
        for _ in range(80):
            if active["id"] not in manager.jobs:
                break
            time.sleep(0.025)
        self.assertNotIn(active["id"], manager.jobs)
        manager.shutdown()

    def test_health_probes_are_cached(self) -> None:
        service = HealthService(ttl_seconds=60)
        with (
            patch.object(service, "_ollama_ready", return_value=True) as ollama,
            patch.object(service, "_scheduler_enabled", return_value=True) as scheduler,
        ):
            self.assertEqual(service.snapshot("http://localhost", "model"), service.snapshot("http://localhost", "model"))
        ollama.assert_called_once()
        scheduler.assert_called_once()

    def test_embedded_scheduler_runs_and_stops(self) -> None:
        called = Event()
        service = SchedulerService(
            checker=called.set,
            interval_seconds=0.05,
            initial_delay_seconds=0.0,
        )
        config = {
            "publishing": {
                "youtube": {"enabled": True},
                "telegram": {"enabled": False},
            },
            "dorama": {"require_review": False},
        }
        with patch("background_services.CONFIG", config):
            self.assertTrue(service.start())
            self.assertTrue(called.wait(1))
            self.assertTrue(service.running)
            service.stop()
        self.assertFalse(service.running)

    def test_embedded_scheduler_respects_manual_review(self) -> None:
        checked = Event()
        service = SchedulerService(
            checker=checked.set,
            interval_seconds=0.05,
            initial_delay_seconds=0.0,
        )
        config = {
            "publishing": {
                "youtube": {"enabled": True},
                "telegram": {"enabled": True},
            },
            "dorama": {"require_review": True},
        }
        with patch("background_services.CONFIG", config):
            service.start()
            time.sleep(0.12)
            service.stop()
        self.assertFalse(checked.is_set())

    def test_frozen_runtime_reenters_bundled_module(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "Dorama Studio Server.exe"),
        ):
            command = python_module_command("yt_dlp", "--version")
        self.assertEqual(
            [
                "Dorama Studio Server.exe",
                "--internal-module",
                "yt_dlp",
                "--version",
            ],
            command,
        )

    def test_runtime_home_can_be_external_to_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.yaml").write_text("paths: {}", encoding="utf-8")
            with patch.dict(os.environ, {"DORAMA_HOME": str(root)}):
                self.assertEqual(root.resolve(), resolve_app_home())


class RestartRecoveryTests(unittest.TestCase):
    @patch("dorama.literal_translation.create_literal_translation")
    @patch("webapp.job_handlers._input_file")
    @patch("webapp.job_handlers._completed_output", return_value=None)
    def test_literal_translation_job_is_restartable(
        self,
        _completed_output,
        input_file,
        create_translation,
    ) -> None:
        source = Path("episode.mp4")
        result = Path("translated.mp4")
        input_file.return_value = source
        create_translation.return_value = result

        actual = execute_persistent_job(
            "job-translation",
            "literal-translation",
            {
                "filename": source.name,
                "start_seconds": 30.0,
                "end_seconds": 210.0,
            },
        )

        self.assertEqual(result, actual)
        create_translation.assert_called_once_with(
            source,
            start_seconds=30.0,
            end_seconds=210.0,
            operation_id="job-translation",
        )

    def test_cancelled_history_is_purged_and_errors_wait_for_manual_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "jobs.db")
            store.initialize()
            cancelled = self._stored_record(status="cancelled")
            failed = self._stored_record(status="failed")
            cancelled["id"] = "cancelled-job"
            failed["id"] = "failed-job"
            store.save(cancelled, {}, resumable=True)
            store.save(failed, {}, resumable=True)

            manager = JobManager(
                store=store,
                persistent_handler=lambda *_args: None,
            )
            manager.start()
            self.assertEqual(["failed-job"], [job["id"] for job in manager.recent(10)])
            self.assertEqual(1, manager.clear_terminal_history())
            self.assertEqual([], manager.recent(10))
            self.assertEqual([], store.load_recent(10))
            manager.shutdown()

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
