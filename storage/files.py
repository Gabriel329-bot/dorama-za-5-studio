"""Безопасные файловые операции для результатов пайплайна."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

_CLIP_ARTIFACT_SUFFIXES = (
    ".ass",
    ".fitted.wav",
    ".plan.json",
    ".qa.txt",
    ".script.txt",
    ".translation.json",
    ".tts-job.json",
)


@dataclass(slots=True)
class StagedDeletion:
    """Файлы, временно перемещённые перед фиксацией удаления в БД."""

    directory: Path | None
    moved: list[tuple[Path, Path]]

    @property
    def file_count(self) -> int:
        return len(self.moved)

    def rollback(self) -> None:
        """Вернуть перемещённые файлы на исходные места."""
        for original, staged in reversed(self.moved):
            if staged.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(original)
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)

    def commit(self) -> None:
        """Окончательно очистить временную корзину после удаления записи."""
        if self.directory is not None:
            shutil.rmtree(self.directory)


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def stage_clip_artifacts_for_deletion(
    media_path: Path,
    *,
    allowed_roots: tuple[Path, ...],
) -> StagedDeletion:
    """Безопасно переместить ролик и его sidecar-файлы во временную корзину.

    Путь из SQLite нельзя считать доверенным: разрешены только результаты внутри
    явно заданных директорий очереди, публикаций и отклонённых роликов.
    """
    resolved_roots = tuple(root.resolve(strict=False) for root in allowed_roots)
    resolved_media = media_path.resolve(strict=False)
    if not _is_within(resolved_media, resolved_roots):
        raise ValueError("Файл ролика находится вне разрешённых каталогов")

    candidates = [resolved_media]
    candidates.extend(resolved_media.with_suffix(suffix) for suffix in _CLIP_ARTIFACT_SUFFIXES)
    existing: list[Path] = []
    for candidate in candidates:
        resolved_candidate = candidate.resolve(strict=False)
        if not _is_within(resolved_candidate, resolved_roots):
            raise ValueError("Связанный файл находится вне разрешённых каталогов")
        if resolved_candidate.is_file():
            existing.append(resolved_candidate)

    if not existing:
        return StagedDeletion(directory=None, moved=[])

    staging_dir = resolved_media.parent / ".trash" / uuid.uuid4().hex
    staging_dir.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for index, source in enumerate(existing):
            staged = staging_dir / f"{index:02d}-{source.name}"
            source.replace(staged)
            moved.append((source, staged))
    except BaseException:
        StagedDeletion(staging_dir, moved).rollback()
        raise
    return StagedDeletion(staging_dir, moved)


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
