"""Memory helpers using plaintext vault storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from constants import CONTEXT_FILE, HISTORY_FILE, MEMORY_FILE, USER_PROFILE_FILE

_CONTEXT_HEADER = "# Current Session"


class SecureMemoryError(RuntimeError):
    """Raised when memory read/write operations fail."""


def save_secure_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(content), encoding="utf-8")


def load_secure_text(path: str | Path, auto_migrate_plaintext: bool = True) -> str:
    _ = auto_migrate_plaintext
    target = Path(path)
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8")


def load_user_profile_dict() -> dict[str, Any]:
    content = load_secure_text(USER_PROFILE_FILE)
    if not content.strip():
        return {}
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise SecureMemoryError(f"User profile parse failed: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise SecureMemoryError("User profile content must be a YAML mapping.")
    return loaded


def save_user_profile_dict(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        raise SecureMemoryError("User profile payload must be a dict.")
    content = yaml.dump(profile, default_flow_style=False, allow_unicode=True, sort_keys=True)
    save_secure_text(USER_PROFILE_FILE, content)


def load_context_notes() -> str:
    content = load_secure_text(CONTEXT_FILE)
    return content if content.strip() else _CONTEXT_HEADER


def save_context_notes(content: str) -> None:
    text = str(content).strip()
    save_secure_text(CONTEXT_FILE, text or _CONTEXT_HEADER)


def append_context_entry(entry: str) -> None:
    text = str(entry).strip()
    if not text:
        return
    current = load_context_notes()
    updated = (current.rstrip() + "\n\n" + text).strip()
    save_context_notes(updated)


def load_memory() -> dict:
    path = Path(MEMORY_FILE)
    if not path.exists():
        return {}
    try:
        content = load_secure_text(path)
    except OSError as exc:
        raise OSError(f"Memory load failed: {exc}") from exc
    return {"project_state": content}


def save_memory(data: dict | str) -> None:
    content = data.get("project_state", "") if isinstance(data, dict) else data
    try:
        save_secure_text(MEMORY_FILE, str(content))
    except OSError as exc:
        raise OSError(f"Memory save failed: {exc}") from exc


def append_memory(entry: str) -> None:
    try:
        current = load_memory().get("project_state", "")
        updated = (current.rstrip() + "\n\n" + entry.strip()).strip()
        save_memory({"project_state": updated})
    except OSError as exc:
        raise OSError(f"Memory append failed: {exc}") from exc


def append_event(event: dict) -> None:
    history_path = Path(HISTORY_FILE)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
