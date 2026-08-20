"""Общие правила запуска исходного и упакованного приложения."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_app_home() -> Path:
    """Найти внешнюю папку с config.yaml и пользовательскими данными."""
    configured = os.environ.get("DORAMA_HOME", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not (root / "config.yaml").is_file():
            raise FileNotFoundError(f"В DORAMA_HOME нет config.yaml: {root}")
        return root

    executable = Path(sys.executable).resolve()
    candidates = [
        executable.parent,
        executable.parent.parent,
        Path.cwd().resolve(),
        Path(__file__).resolve().parent,
    ]
    visited: set[Path] = set()
    for candidate in candidates:
        if candidate in visited:
            continue
        visited.add(candidate)
        if (candidate / "config.yaml").is_file():
            return candidate
    raise FileNotFoundError(
        "Не найден config.yaml. Положите EXE в папку проекта или задайте DORAMA_HOME."
    )


def python_module_command(module: str, *arguments: str) -> list[str]:
    """Повторно вызвать модуль и из Python, и из PyInstaller EXE."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-module", module, *arguments]
    return [sys.executable, "-m", module, *arguments]
