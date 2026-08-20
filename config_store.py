"""Атомарное точечное обновление config.yaml без удаления комментариев."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from threading import Lock
from typing import Any, TypeAlias

import yaml

ConfigPath: TypeAlias = tuple[str, ...]
ConfigUpdates: TypeAlias = Mapping[ConfigPath, object]
ConfigDict: TypeAlias = dict[str, Any]
_WRITE_LOCK = Lock()


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _index_key_paths(lines: list[str]) -> dict[ConfigPath, tuple[int, int]]:
    """Построить индекс всех YAML-ключей за один проход O(L)."""
    stack: dict[int, str] = {}
    result: dict[ConfigPath, tuple[int, int]] = {}
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("-") or ":" not in stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            continue
        depth = indent // 2
        key = stripped.split(":", 1)[0].strip().strip("\"'")
        stack[depth] = key
        for stale_depth in tuple(item for item in stack if item > depth):
            del stack[stale_depth]
        path = tuple(stack[item] for item in range(depth + 1) if item in stack)
        result[path] = (index, indent)
    return result


def _replacement_span(lines: list[str], index: int, indent: int) -> tuple[int, int]:
    """Вернуть границы leaf-значения, включая старый блочный список."""
    end = index + 1
    original_value = lines[index].split(":", 1)[1]
    if original_value.split("#", 1)[0].strip():
        return index, end
    while end < len(lines):
        candidate = lines[end]
        stripped = candidate.strip()
        candidate_indent = len(candidate) - len(candidate.lstrip(" "))
        if stripped.startswith("-") and candidate_indent >= indent:
            end += 1
            continue
        break
    return index, end


def update_yaml_text(text: str, updates: ConfigUpdates) -> str:
    """Обновить известные leaf-ключи за O(L + U log U)."""
    lines = text.splitlines()
    index_by_path = _index_key_paths(lines)
    replacements: list[tuple[int, int, str]] = []
    for path, value in updates.items():
        if not path or path not in index_by_path:
            raise KeyError(".".join(path))
        index, indent = index_by_path[path]
        line = lines[index]
        comment_at = line.find("  #")
        comment = line[comment_at:] if comment_at >= 0 else ""
        replacement = f"{' ' * indent}{path[-1]}: {_format_value(value)}{comment}"
        start, end = _replacement_span(lines, index, indent)
        replacements.append((start, end, replacement))

    # Обратный порядок сохраняет исходные индексы при удалении блочных списков.
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]

    result = "\n".join(lines) + "\n"
    parsed = yaml.safe_load(result)
    if not isinstance(parsed, dict):
        raise ValueError(  # noqa: TRY004 — это ошибка структуры конфигурации
            "После обновления config.yaml должен содержать объект"
        )
    return result


def update_config_file(path: Path, updates: ConfigUpdates) -> ConfigDict:
    """Записать валидный YAML через уникальный temp-файл и os.replace."""
    with _WRITE_LOCK:
        updated_text = update_yaml_text(path.read_text(encoding="utf-8"), updates)
        parsed = yaml.safe_load(updated_text)
        if not isinstance(parsed, dict):
            raise ValueError(  # noqa: TRY004 — это ошибка структуры конфигурации
                "После обновления config.yaml должен содержать объект"
            )

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
                temporary.write(updated_text)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return parsed


def apply_config_snapshot(target: MutableMapping[str, Any], source: Mapping[str, Any]) -> None:
    """Заменить верхнеуровневые секции без временного состояния пустого CONFIG."""
    for key, value in source.items():
        target[key] = value
    for stale_key in tuple(key for key in target if key not in source):
        del target[stale_key]
