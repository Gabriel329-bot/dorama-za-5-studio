"""Безопасные файловые операции для результатов пайплайна."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path


def move_to_unique(source: Path, destination_dir: Path) -> Path:
    """Переместить файл, не перезаписывая существующий результат."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}_{uuid.uuid4().hex[:8]}{source.suffix}"
    shutil.move(str(source), str(destination))
    return destination


def atomic_write_text(path: Path, content: str, *, private: bool = False) -> None:
    """Записать текст без окна с частичным файлом; секреты ограничить chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        if private:
            os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
