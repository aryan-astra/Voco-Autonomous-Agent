"""Flat-file memory helpers for VOCO."""

from __future__ import annotations

import json
from pathlib import Path

from constants import HISTORY_FILE, MEMORY_FILE


def load_memory() -> dict:
    """Load memory snapshot from project_state.md into a dict payload."""
    path = Path(MEMORY_FILE)
    if not path.exists():
        return {}
    return {"project_state": path.read_text(encoding="utf-8")}


def save_memory(data: dict | str) -> None:
    """Persist memory payload to project_state.md."""
    path = Path(MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, dict):
        content = data.get("project_state", "")
        path.write_text(str(content), encoding="utf-8")
        return
    path.write_text(str(data), encoding="utf-8")


def append_memory(entry: str) -> None:
    """Append free-text content to project_state.md."""
    current = load_memory().get("project_state", "")
    updated = (current.rstrip() + "\n\n" + entry.strip()).strip()
    save_memory({"project_state": updated})


def append_event(event: dict) -> None:
    """Append a JSON event record to HISTORY.jsonl."""
    history_path = Path(HISTORY_FILE)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
