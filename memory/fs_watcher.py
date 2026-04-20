"""Background filesystem watcher for passive user-profile activity signals."""

from __future__ import annotations

import atexit
import importlib.util
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from constants import DB_WAL_MODE, WATCHDOG_DEBOUNCE_SEC, WORKSPACE_PATH

_USER_PROFILE_MODULE_PATH = Path(__file__).with_name("user_profile.py")
_PROJECT_MONITOR_ROOT = WORKSPACE_PATH.resolve()
_PROFILE_FOLDERS = ("Desktop", "Documents")

_IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "$recycle.bin",
    "system volume information",
}
_IGNORED_FILE_NAMES = {
    "desktop.ini",
    "thumbs.db",
    "user_profile.db",
    "user_profile.db-wal",
    "user_profile.db-shm",
    "file_index.db",
    "app_index.db",
}
_IGNORED_SUFFIXES = {
    ".tmp",
    ".temp",
    ".swp",
    ".part",
    ".crdownload",
    ".log",
    ".db-wal",
    ".db-shm",
}

_user_profile_class: type | None = None
_user_profile_class_attempted = False
_user_profile_store: object | None = None
_user_profile_store_attempted = False
_watcher_observer: object | None = None
_watcher_handler: "_UserProfileWatcherHandler | None" = None
_watcher_lock = threading.Lock()
_atexit_registered = False

try:
    from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:
    Observer = None

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    FileSystemEvent = Any  # type: ignore[assignment]
    FileMovedEvent = Any  # type: ignore[assignment]


def _observer_is_alive(observer: object | None) -> bool:
    if observer is None:
        return False
    is_alive = getattr(observer, "is_alive", None)
    if callable(is_alive):
        try:
            return bool(is_alive())
        except Exception:
            return False
    return True


def _normalize_path(raw_path: str | Path | None) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        return Path(text).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(text)


def _is_under_monitored_roots(path: Path, monitored_roots: list[Path]) -> bool:
    normalized = str(path).rstrip("\\/").lower()
    for root in monitored_roots:
        root_text = str(root).rstrip("\\/").lower()
        if normalized == root_text or normalized.startswith(f"{root_text}\\"):
            return True
    return False


def _is_ignored_path(path: Path) -> bool:
    lower_parts = [part.lower() for part in path.parts]
    if any(part in _IGNORED_DIR_NAMES for part in lower_parts):
        return True
    name = path.name.lower()
    if name in _IGNORED_FILE_NAMES:
        return True
    protected_prefixes = (
        str(Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()).lower(),
        str(Path(r"C:\Program Files").resolve()).lower(),
        str(Path(r"C:\Program Files (x86)").resolve()).lower(),
        str(Path(os.environ.get("APPDATA", r"C:\Users\Default\AppData\Roaming")).resolve()).lower(),
        str(Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local")).resolve()).lower(),
    )
    normalized = str(path).lower()
    if any(normalized.startswith(prefix) for prefix in protected_prefixes):
        return True
    if any(name.endswith(suffix) for suffix in _IGNORED_SUFFIXES):
        return True
    return False


def get_monitored_paths() -> list[Path]:
    roots: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE", "")).expanduser()
    for folder in _PROFILE_FOLDERS:
        candidate = _normalize_path(user_profile / folder)
        if candidate is not None and candidate.exists():
            roots.append(candidate)
    project_root = _normalize_path(_PROJECT_MONITOR_ROOT)
    if project_root is not None and project_root.exists():
        roots.append(project_root)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _load_user_profile_class() -> type | None:
    global _user_profile_class_attempted, _user_profile_class
    if _user_profile_class is not None:
        return _user_profile_class
    if _user_profile_class_attempted:
        return None

    _user_profile_class_attempted = True
    if not _USER_PROFILE_MODULE_PATH.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("voco_fs_watcher_user_profile", _USER_PROFILE_MODULE_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = getattr(module, "UserProfile", None)
        if isinstance(candidate, type):
            _user_profile_class = candidate
    except Exception:
        _user_profile_class = None
    return _user_profile_class


def _get_user_profile_store() -> object | None:
    global _user_profile_store_attempted, _user_profile_store
    if _user_profile_store is not None:
        return _user_profile_store
    if _user_profile_store_attempted:
        return None

    _user_profile_store_attempted = True
    profile_class = _load_user_profile_class()
    if profile_class is None:
        return None
    try:
        _user_profile_store = profile_class()
    except Exception:
        _user_profile_store = None
    return _user_profile_store


class _UserProfileWatcherHandler(FileSystemEventHandler):
    def __init__(self, profile: object, monitored_roots: list[Path]) -> None:
        super().__init__()
        self._profile = profile
        self._monitored_roots = monitored_roots
        self._recent_events: dict[tuple[str, str], float] = {}
        self._recent_lock = threading.Lock()
        self._pending_events: list[dict[str, Any]] = []
        self._pending_lock = threading.Lock()
        self._flush_stop = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        self._ensure_profile_wal_mode()

    def _is_duplicate_event(self, action: str, path: str) -> bool:
        now = time.monotonic()
        key = (action.lower(), path.lower())
        with self._recent_lock:
            previous = self._recent_events.get(key)
            self._recent_events[key] = now
            if len(self._recent_events) > 5000:
                oldest_items = sorted(self._recent_events.items(), key=lambda item: item[1])[:4000]
                for stale_key, _ in oldest_items:
                    self._recent_events.pop(stale_key, None)
        return previous is not None and (now - previous) < 0.35

    def _ensure_profile_wal_mode(self) -> None:
        if not DB_WAL_MODE:
            return
        connect_method = getattr(self._profile, "_connect", None)
        if not callable(connect_method):
            return
        try:
            conn = connect_method()
        except Exception:
            return
        try:
            if isinstance(conn, sqlite3.Connection):
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _flush_loop(self) -> None:
        debounce_sec = max(1, int(WATCHDOG_DEBOUNCE_SEC))
        while not self._flush_stop.wait(0.5):
            self._flush_pending_events(force=False, debounce_sec=debounce_sec)
        self._flush_pending_events(force=True, debounce_sec=debounce_sec)

    def _flush_pending_events(self, *, force: bool, debounce_sec: int) -> None:
        cutoff = time.monotonic() - debounce_sec
        with self._pending_lock:
            if not self._pending_events:
                return
            if not force:
                ready_count = 0
                for payload in self._pending_events:
                    if float(payload.get("queued_at", 0.0)) <= cutoff:
                        ready_count += 1
                    else:
                        break
                if ready_count == 0:
                    return
                ready = self._pending_events[:ready_count]
                self._pending_events = self._pending_events[ready_count:]
            else:
                ready = self._pending_events
                self._pending_events = []

        record_method = getattr(self._profile, "record_file_activity", None)
        if not callable(record_method):
            return
        for payload in ready:
            try:
                record_method(
                    path=str(payload["path"]),
                    action=str(payload["action"]),
                    tool_name="watchdog.fs_watcher",
                    status="observed",
                    metadata=dict(payload["metadata"]),
                )
            except Exception:
                continue

    def close(self) -> None:
        self._flush_stop.set()
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2)

    def _record_activity(self, path_value: str | Path | None, action: str, metadata: dict[str, Any] | None = None) -> None:
        normalized_path = _normalize_path(path_value)
        if normalized_path is None:
            return
        if not _is_under_monitored_roots(normalized_path, self._monitored_roots):
            return
        if _is_ignored_path(normalized_path):
            return

        path_text = str(normalized_path)
        if self._is_duplicate_event(action, path_text):
            return

        payload = {"source": "watchdog"}
        if isinstance(metadata, dict):
            payload.update(metadata)
        with self._pending_lock:
            self._pending_events.append(
                {
                    "path": path_text,
                    "action": action,
                    "metadata": payload,
                    "queued_at": time.monotonic(),
                }
            )

    def on_created(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        self._record_activity(getattr(event, "src_path", ""), "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        self._record_activity(getattr(event, "src_path", ""), "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        self._record_activity(getattr(event, "src_path", ""), "deleted")

    def on_moved(self, event: FileMovedEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        src_path = getattr(event, "src_path", "")
        dest_path = getattr(event, "dest_path", "")
        self._record_activity(dest_path, "moved", metadata={"source_path": src_path})


def start_filesystem_watcher() -> object | None:
    global _watcher_observer, _watcher_handler, _atexit_registered
    if Observer is None:
        return None

    with _watcher_lock:
        if _observer_is_alive(_watcher_observer):
            return _watcher_observer

        profile = _get_user_profile_store()
        if profile is None:
            return None

        monitored_paths = get_monitored_paths()
        if not monitored_paths:
            return None

        observer = Observer()
        handler = _UserProfileWatcherHandler(profile=profile, monitored_roots=monitored_paths)
        scheduled_roots = 0
        for root in monitored_paths:
            try:
                observer.schedule(handler, str(root), recursive=True)
                scheduled_roots += 1
            except Exception:
                continue
        if scheduled_roots == 0:
            return None

        try:
            observer.start()
        except Exception:
            try:
                observer.stop()
            except Exception:
                pass
            return None

        _watcher_observer = observer
        _watcher_handler = handler
        if not _atexit_registered:
            atexit.register(stop_filesystem_watcher)
            _atexit_registered = True
        return observer


def stop_filesystem_watcher(observer: object | None = None) -> None:
    global _watcher_observer, _watcher_handler
    if _watcher_handler is not None:
        try:
            _watcher_handler.close()
        except Exception:
            pass
        _watcher_handler = None

    target = observer if observer is not None else _watcher_observer
    if target is None:
        return

    stop_method = getattr(target, "stop", None)
    if callable(stop_method):
        try:
            stop_method()
        except Exception:
            pass

    join_method = getattr(target, "join", None)
    if callable(join_method):
        try:
            join_method(timeout=3)
        except Exception:
            pass

    if target is _watcher_observer or observer is None:
        _watcher_observer = None


__all__ = ["get_monitored_paths", "start_filesystem_watcher", "stop_filesystem_watcher"]
