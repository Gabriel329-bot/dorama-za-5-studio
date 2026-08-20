"""Единый Windows-лаунчер локальной студии и всех фоновых сервисов."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from threading import Event, Thread
from typing import Any
from urllib.parse import urlparse

import requests

from runtime_support import resolve_app_home

APP_TITLE = "Dorama Studio Server"
DASHBOARD_URL = "http://127.0.0.1:8765"
MUTEX_NAME = "Local\\DoramaStudioServer-9e5582ba"
log = logging.getLogger("dorama-server")


def _internal_module(module: str, arguments: list[str]) -> int:
    if module != "yt_dlp":
        raise ValueError(f"Внутренний модуль не разрешён: {module}")
    from yt_dlp import main as yt_dlp_main  # type: ignore[import-untyped]

    try:
        result = yt_dlp_main(arguments)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result or 0)


def _acquire_single_instance() -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ctypes.windll.kernel32
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = create_mutex(None, False, MUTEX_NAME)
    if not handle:
        raise OSError("Windows не смог создать блокировку приложения")
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _release_single_instance(handle: int | None) -> None:
    if os.name == "nt" and handle is not None:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(int(handle)))


def _configure_file_logging(root: Path) -> None:
    log_dir = root / "storage" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(
        log_dir / "studio-server.log",
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


class OllamaManager:
    def __init__(self, host: str, model: str, root: Path) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.root = root
        self.process: subprocess.Popen[bytes] | None = None

    def _tags(self) -> dict[str, Any] | None:
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=(0.5, 2.0))
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (requests.RequestException, TypeError, ValueError):
            return None

    @property
    def ready(self) -> bool:
        return self._tags() is not None

    @property
    def model_ready(self) -> bool:
        payload = self._tags()
        if payload is None:
            return False
        models = {
            str(item.get("name") or "")
            for item in payload.get("models") or []
            if isinstance(item, dict)
        }
        return self.model in models or any(
            name.split(":")[0] == self.model.split(":")[0]
            for name in models
            if name
        )

    def ensure_started(self, timeout: float = 25.0) -> bool:
        """Вернуть True, только если Ollama была запущена этим приложением."""
        if self.ready:
            return False
        host = (urlparse(self.host).hostname or "").lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            log.warning("Ollama настроена на внешний адрес; автозапуск пропущен")
            return False
        executable = self._find_executable()
        if executable is None:
            log.warning("ollama.exe не найдена; установите Ollama отдельно")
            return False
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(executable), "serve"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready:
                log.info("Ollama запущена приложением")
                return True
            if self.process.poll() is not None:
                break
            time.sleep(0.25)
        self.stop()
        log.error("Ollama не запустилась за %.0f секунд", timeout)
        return False

    def _find_executable(self) -> Path | None:
        configured = os.environ.get("OLLAMA_EXE", "").strip()
        candidates = [
            Path(configured).expanduser() if configured else None,
            Path(found) if (found := shutil.which("ollama")) else None,
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Ollama"
            / "ollama.exe",
        ]
        return next(
            (candidate.resolve() for candidate in candidates if candidate and candidate.is_file()),
            None,
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class ServerController:
    def __init__(self, root: Path) -> None:
        from settings import CONFIG

        self.root = root
        self.ollama = OllamaManager(
            str(CONFIG["highlight"]["ollama_host"]),
            str(CONFIG["highlight"]["model"]),
            root,
        )
        self.thread: Thread | None = None
        self.server: Any | None = None
        self.started = Event()
        self.stopped = Event()
        self.failure: str | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.started.clear()
        self.stopped.clear()
        self.failure = None
        self.thread = Thread(target=self._run, name="studio-api", daemon=False)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.ollama.ensure_started()
            import uvicorn

            from webapp.app import app

            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=8765,
                log_level="info",
                access_log=False,
                log_config=None,
            )
            self.server = uvicorn.Server(config)
            self.started.set()
            asyncio.run(self.server.serve())
        except Exception as exc:
            self.failure = str(exc)
            log.exception("Сервер остановлен из-за ошибки")
        finally:
            self.started.set()
            self.stopped.set()

    @property
    def running(self) -> bool:
        return bool(
            self.thread is not None
            and self.thread.is_alive()
            and self.server is not None
            and not self.failure
        )

    def wait_until_ready(self, timeout: float = 40.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.failure:
                return False
            try:
                response = requests.get(f"{DASHBOARD_URL}/api/session", timeout=1.0)
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            if self.stopped.wait(0.2):
                return False
        return False

    def dashboard(self) -> dict[str, Any] | None:
        try:
            response = requests.get(f"{DASHBOARD_URL}/api/dashboard", timeout=2.0)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (requests.RequestException, TypeError, ValueError):
            return None

    def stop(self, timeout: float = 45.0) -> None:
        server = self.server
        if server is not None:
            server.should_exit = True
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            log.error("Сервер не завершился за %.0f секунд", timeout)
        self.ollama.stop()


class QueueLogHandler(logging.Handler):
    def __init__(self, messages: queue.Queue[str]) -> None:
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.put_nowait(self.format(record))
        except (ValueError, queue.Full):
            pass


class ServerWindow:
    def __init__(self, controller: ServerController, *, open_browser: bool) -> None:
        import tkinter as tk
        from tkinter import messagebox

        self.tk = tk
        self.messagebox = messagebox
        self.controller = controller
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("760x560")
        self.root.minsize(660, 480)
        self.root.configure(bg="#120c0f")
        self.root.protocol("WM_DELETE_WINDOW", self._request_exit)
        self.status_vars = {
            name: tk.StringVar(value="Запускается…")
            for name in ("server", "ollama", "bot", "scheduler", "jobs")
        }
        self.log_messages: queue.Queue[str] = queue.Queue(maxsize=500)
        self._closing = False
        self._browser_requested = open_browser
        self._browser_opened = False
        self._build_ui()
        self._install_logging()
        self.controller.start()
        self.root.after(250, self._refresh)

    def _build_ui(self) -> None:
        tk = self.tk
        header = tk.Frame(self.root, bg="#201419", padx=24, pady=20)
        header.pack(fill="x")
        tk.Label(
            header,
            text="ДОРАМА ЗА 5",
            fg="#e4bd77",
            bg="#201419",
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Studio Server",
            fg="#fff4e6",
            bg="#201419",
            font=("Georgia", 28),
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            header,
            text="Панель, задачи, Telegram-бот, планировщик и Ollama",
            fg="#9d8d92",
            bg="#201419",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self.root, bg="#120c0f", padx=24, pady=18)
        body.pack(fill="both", expand=True)
        statuses = tk.Frame(body, bg="#120c0f")
        statuses.pack(fill="x")
        labels = {
            "server": "Локальный API",
            "ollama": "Ollama / Qwen",
            "bot": "Telegram-бот",
            "scheduler": "Планировщик",
            "jobs": "Активные процессы",
        }
        for index, (key, title) in enumerate(labels.items()):
            card = tk.Frame(
                statuses,
                bg="#24191d",
                padx=13,
                pady=10,
                highlightbackground="#3c2d32",
                highlightthickness=1,
            )
            card.grid(row=index // 3, column=index % 3, padx=4, pady=4, sticky="nsew")
            statuses.grid_columnconfigure(index % 3, weight=1)
            tk.Label(card, text=title, fg="#9d8d92", bg="#24191d", font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(card, textvariable=self.status_vars[key], fg="#f4dfbd", bg="#24191d", font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(3, 0))

        actions = tk.Frame(body, bg="#120c0f", pady=12)
        actions.pack(fill="x")
        self._button(actions, "Открыть панель", self._open_dashboard, "#805232").pack(side="left", padx=(0, 8))
        self._button(actions, "Обновить статус", self._refresh_now, "#2d554a").pack(side="left", padx=8)
        self._button(actions, "Остановить всё", self._request_exit, "#662b39").pack(side="right")

        tk.Label(body, text="Журнал сервера", fg="#b7a5aa", bg="#120c0f", font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(2, 6))
        self.log_view = tk.Text(
            body,
            height=12,
            bg="#0b0809",
            fg="#cfc2c6",
            insertbackground="#cfc2c6",
            relief="flat",
            padx=10,
            pady=8,
            font=("Consolas", 8),
            state="disabled",
            wrap="word",
        )
        self.log_view.pack(fill="both", expand=True)

    def _button(self, parent: Any, text: str, command: Any, color: str) -> Any:
        return self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="#fff7ed",
            activebackground=color,
            activeforeground="#ffffff",
            relief="flat",
            padx=15,
            pady=8,
            cursor="hand2",
            font=("Segoe UI Semibold", 9),
        )

    def _install_logging(self) -> None:
        handler = QueueLogHandler(self.log_messages)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    def _refresh_now(self) -> None:
        self._update_status()
        self._drain_logs()

    def _refresh(self) -> None:
        if self._closing:
            return
        self._update_status()
        self._drain_logs()
        self.root.after(1200, self._refresh)

    def _update_status(self) -> None:
        dashboard = self.controller.dashboard()
        self.status_vars["server"].set("Работает" if dashboard else (self.controller.failure or "Запускается…"))
        self.status_vars["ollama"].set(
            "Модель готова"
            if self.controller.ollama.model_ready
            else ("Сервер без модели" if self.controller.ollama.ready else "Не запущена")
        )
        if dashboard:
            system = dashboard.get("system") or {}
            self.status_vars["bot"].set("Работает" if system.get("telegram_bot") else "Не настроен")
            self.status_vars["scheduler"].set("Работает" if system.get("scheduler") else "Остановлен")
            active = [
                job
                for job in dashboard.get("jobs") or []
                if job.get("status") in {"queued", "running", "cancelling", "paused"}
            ]
            self.status_vars["jobs"].set(str(len(active)))
            if self._browser_requested and not self._browser_opened:
                self._browser_opened = True
                self._open_dashboard()
        else:
            self.status_vars["bot"].set("Ожидает API")
            self.status_vars["scheduler"].set("Ожидает API")
            self.status_vars["jobs"].set("—")

    def _drain_logs(self) -> None:
        lines: list[str] = []
        while len(lines) < 80:
            try:
                lines.append(self.log_messages.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_view.configure(state="normal")
        self.log_view.insert("end", "\n".join(lines) + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    @staticmethod
    def _open_dashboard() -> None:
        webbrowser.open(DASHBOARD_URL)

    def _request_exit(self) -> None:
        if self._closing:
            return
        if not self.messagebox.askyesno(
            APP_TITLE,
            "Остановить сервер, бота и планировщик?\n"
            "Текущие задачи будут приостановлены и продолжатся после запуска.",
            parent=self.root,
        ):
            return
        self._closing = True
        self.status_vars["server"].set("Останавливается…")
        Thread(target=self._stop_and_close, name="studio-stop", daemon=True).start()

    def _stop_and_close(self) -> None:
        self.controller.stop()
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def _run_headless(
    controller: ServerController,
    *,
    open_browser: bool,
    run_seconds: float | None,
) -> int:
    stopping = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    controller.start()
    if not controller.wait_until_ready():
        controller.stop()
        failure = controller.failure or "Локальный API не запустился"
        log.error(failure)
        print(failure, file=sys.stderr)
        return 1
    if open_browser:
        webbrowser.open(DASHBOARD_URL)
    print(f"{APP_TITLE}: {DASHBOARD_URL}")
    deadline = (
        time.monotonic() + run_seconds
        if run_seconds is not None and run_seconds > 0
        else None
    )
    while not stopping.wait(0.5):
        if controller.stopped.is_set():
            return 1
        if deadline is not None and time.monotonic() >= deadline:
            break
    controller.stop()
    return 0


def _parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--internal-module", nargs=argparse.REMAINDER)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = _parse_args(list(arguments if arguments is not None else sys.argv[1:]))
    if args.internal_module:
        module, *module_args = args.internal_module
        return _internal_module(module, module_args)

    root = resolve_app_home()
    os.environ["DORAMA_HOME"] = str(root)
    os.chdir(root)
    _configure_file_logging(root)
    instance = _acquire_single_instance()
    if instance is None:
        webbrowser.open(DASHBOARD_URL)
        return 0
    try:
        controller = ServerController(root)
        if args.headless:
            return _run_headless(
                controller,
                open_browser=not args.no_browser,
                run_seconds=args.run_seconds,
            )
        window = ServerWindow(controller, open_browser=not args.no_browser)
        window.run()
        return 0
    finally:
        _release_single_instance(instance)


if __name__ == "__main__":
    raise SystemExit(main())
