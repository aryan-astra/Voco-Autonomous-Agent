"""SQLite-backed local user profiling for VOCO runtime signals."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from constants import BASE_DIR, MEMORY_DIR

_DEFAULT_DB_NAME = "user_profile.db"
_DEFAULT_TIMESTAMP_COLUMN = "event_time"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_file_activity_time ON file_activity(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_file_activity_path ON file_activity(path);

CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    command TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_command_history_time ON command_history(event_time DESC);

CREATE TABLE IF NOT EXISTS app_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    app_name TEXT NOT NULL,
    action TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_app_usage_time ON app_usage(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_app_usage_name ON app_usage(app_name);

CREATE TABLE IF NOT EXISTS failure_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    message TEXT NOT NULL,
    known_fix TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_failure_memory_time ON failure_memory(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_failure_memory_class ON failure_memory(failure_class);

CREATE TABLE IF NOT EXISTS preferences (
    pref_key TEXT PRIMARY KEY,
    pref_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS learned_recipes (
    recipe_key TEXT PRIMARY KEY,
    recipe_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
"""


class UserProfileError(RuntimeError):
    """Raised when user profile persistence fails."""


class UserProfile:
    """Local SQLite-backed profile memory for runtime personalization signals."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.project_root = BASE_DIR.resolve()
        self.memory_root = MEMORY_DIR.resolve()
        self.db_path = self._resolve_db_path(db_path)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _resolve_db_path(self, db_path: str | Path | None) -> Path:
        default_path = (self.memory_root / _DEFAULT_DB_NAME).resolve()
        candidate = Path(db_path) if db_path else default_path
        if not candidate.is_absolute():
            candidate = self.memory_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError:
            return default_path
        return resolved

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        except sqlite3.Error as exc:
            raise UserProfileError(f"Failed to open profile DB '{self.db_path}': {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.executescript(_SCHEMA_SQL)
            except sqlite3.Error as exc:
                raise UserProfileError(f"Failed to initialize profile schema: {exc}") from exc

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _coerce_timestamp(self, value: str | datetime | None, column_name: str = _DEFAULT_TIMESTAMP_COLUMN) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        _ = column_name
        return self._now_iso()

    @staticmethod
    def _normalize_text(value: Any, *, fallback: str = "", max_length: int = 0) -> str:
        text = str(value if value is not None else fallback).strip()
        if not text:
            text = fallback
        if max_length > 0 and len(text) > max_length:
            return text[:max_length]
        return text

    @staticmethod
    def _normalize_limit(limit: int, *, default: int = 20, max_limit: int = 200) -> int:
        try:
            parsed = int(limit)
        except (TypeError, ValueError):
            return default
        return max(1, min(max_limit, parsed))

    @staticmethod
    def _serialize_metadata(metadata: Any) -> str:
        if metadata is None:
            return "{}"
        if isinstance(metadata, str):
            stripped = metadata.strip()
            if not stripped:
                return "{}"
            try:
                json.loads(stripped)
                return stripped
            except json.JSONDecodeError:
                return json.dumps({"raw": stripped}, ensure_ascii=False)
        try:
            return json.dumps(metadata, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return json.dumps({"raw": str(metadata)}, ensure_ascii=False)

    @staticmethod
    def _decode_metadata(raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        text = raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def _write(self, query: str, params: tuple[Any, ...]) -> int:
        with self._lock:
            try:
                with self._connect() as conn:
                    cursor = conn.execute(query, params)
                    return int(cursor.lastrowid or 0)
            except sqlite3.Error as exc:
                raise UserProfileError(f"Profile write failed: {exc}") from exc

    def _query_rows(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._lock:
            try:
                with self._connect() as conn:
                    rows = conn.execute(query, params).fetchall()
            except sqlite3.Error as exc:
                raise UserProfileError(f"Profile read failed: {exc}") from exc
        records: list[dict[str, Any]] = []
        for row in rows:
            payload = {key: row[key] for key in row.keys()}
            if "metadata" in payload:
                payload["metadata"] = self._decode_metadata(payload["metadata"])
            records.append(payload)
        return records

    def record_file_activity(
        self,
        path: str,
        action: str,
        *,
        tool_name: str = "",
        status: str = "",
        metadata: Any = None,
        event_time: str | datetime | None = None,
    ) -> int:
        normalized_path = self._normalize_text(path, max_length=1200)
        if not normalized_path:
            raise ValueError("path is required")
        normalized_action = self._normalize_text(action, fallback="unknown", max_length=120)
        return self._write(
            """
            INSERT INTO file_activity (event_time, path, action, tool_name, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._coerce_timestamp(event_time),
                normalized_path,
                normalized_action,
                self._normalize_text(tool_name, fallback="unknown", max_length=120),
                self._normalize_text(status, fallback="unknown", max_length=60),
                self._serialize_metadata(metadata),
            ),
        )

    def get_file_activity(self, *, limit: int = 20, path_contains: str | None = None) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if path_contains and path_contains.strip():
            filters.append("path LIKE ?")
            params.append(f"%{path_contains.strip()}%")
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(self._normalize_limit(limit))
        return self._query_rows(
            f"""
            SELECT id, event_time, path, action, tool_name, status, metadata
            FROM file_activity
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def record_command(
        self,
        command: str,
        *,
        status: str = "",
        tool_name: str = "",
        metadata: Any = None,
        event_time: str | datetime | None = None,
    ) -> int:
        normalized_command = self._normalize_text(command, max_length=4000)
        if not normalized_command:
            raise ValueError("command is required")
        return self._write(
            """
            INSERT INTO command_history (event_time, command, tool_name, status, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                self._coerce_timestamp(event_time),
                normalized_command,
                self._normalize_text(tool_name, fallback="orchestrator", max_length=120),
                self._normalize_text(status, fallback="unknown", max_length=60),
                self._serialize_metadata(metadata),
            ),
        )

    def get_command_history(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if status and status.strip():
            filters.append("status = ?")
            params.append(status.strip())
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(self._normalize_limit(limit))
        return self._query_rows(
            f"""
            SELECT id, event_time, command, tool_name, status, metadata
            FROM command_history
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def record_app_usage(
        self,
        app_name: str,
        action: str,
        *,
        status: str = "",
        tool_name: str = "",
        metadata: Any = None,
        event_time: str | datetime | None = None,
    ) -> int:
        normalized_app = self._normalize_text(app_name, max_length=260)
        if not normalized_app:
            raise ValueError("app_name is required")
        normalized_action = self._normalize_text(action, fallback="unknown", max_length=120)
        return self._write(
            """
            INSERT INTO app_usage (event_time, app_name, action, tool_name, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._coerce_timestamp(event_time),
                normalized_app,
                normalized_action,
                self._normalize_text(tool_name, fallback="unknown", max_length=120),
                self._normalize_text(status, fallback="unknown", max_length=60),
                self._serialize_metadata(metadata),
            ),
        )

    def get_app_usage(self, *, limit: int = 20, app_name: str | None = None) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if app_name and app_name.strip():
            filters.append("app_name = ?")
            params.append(app_name.strip())
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(self._normalize_limit(limit))
        return self._query_rows(
            f"""
            SELECT id, event_time, app_name, action, tool_name, status, metadata
            FROM app_usage
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def record_failure(
        self,
        failure_class: str,
        message: str,
        *,
        tool_name: str = "",
        known_fix: str = "",
        metadata: Any = None,
        event_time: str | datetime | None = None,
    ) -> int:
        normalized_failure = self._normalize_text(failure_class, fallback="unknown", max_length=200)
        normalized_message = self._normalize_text(message, fallback="unknown", max_length=4000)
        return self._write(
            """
            INSERT INTO failure_memory (event_time, failure_class, tool_name, message, known_fix, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._coerce_timestamp(event_time),
                normalized_failure,
                self._normalize_text(tool_name, fallback="unknown", max_length=120),
                normalized_message,
                self._normalize_text(known_fix, max_length=1000),
                self._serialize_metadata(metadata),
            ),
        )

    def get_failure_memory(
        self,
        *,
        limit: int = 20,
        failure_class: str | None = None,
        tool_name: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if failure_class and failure_class.strip():
            filters.append("failure_class = ?")
            params.append(failure_class.strip())
        if tool_name and tool_name.strip():
            filters.append("tool_name = ?")
            params.append(tool_name.strip())
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(self._normalize_limit(limit))
        return self._query_rows(
            f"""
            SELECT id, event_time, failure_class, tool_name, message, known_fix, metadata
            FROM failure_memory
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def set_preference(
        self,
        key: str,
        value: Any,
        *,
        metadata: Any = None,
        updated_at: str | datetime | None = None,
    ) -> None:
        normalized_key = self._normalize_text(key, max_length=200)
        if not normalized_key:
            raise ValueError("preference key is required")
        if isinstance(value, (dict, list, tuple)):
            normalized_value = json.dumps(value, ensure_ascii=False, default=str)
        else:
            normalized_value = self._normalize_text(value, max_length=4000)
        timestamp = self._coerce_timestamp(updated_at, column_name="updated_at")
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO preferences (pref_key, pref_value, updated_at, metadata)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(pref_key) DO UPDATE SET
                            pref_value = excluded.pref_value,
                            updated_at = excluded.updated_at,
                            metadata = excluded.metadata
                        """,
                        (normalized_key, normalized_value, timestamp, self._serialize_metadata(metadata)),
                    )
            except sqlite3.Error as exc:
                raise UserProfileError(f"Preference update failed: {exc}") from exc

    def get_preference(self, key: str, default: Any = None) -> Any:
        normalized_key = self._normalize_text(key, max_length=200)
        if not normalized_key:
            return default
        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT pref_value FROM preferences WHERE pref_key = ?",
                        (normalized_key,),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise UserProfileError(f"Preference read failed: {exc}") from exc
        if row is None:
            return default
        return row["pref_value"]

    def get_preferences(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = ()
        query = "SELECT pref_key, pref_value, updated_at, metadata FROM preferences ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params = (self._normalize_limit(limit),)
        return self._query_rows(query, params)

    def record_learned_recipe(
        self,
        recipe_key: str,
        recipe_text: str,
        *,
        success: bool | None = None,
        metadata: Any = None,
        updated_at: str | datetime | None = None,
    ) -> None:
        normalized_key = self._normalize_text(recipe_key, max_length=300)
        if not normalized_key:
            raise ValueError("recipe_key is required")
        normalized_recipe = self._normalize_text(recipe_text, max_length=4000)
        if not normalized_recipe:
            raise ValueError("recipe_text is required")
        timestamp = self._coerce_timestamp(updated_at, column_name="updated_at")
        last_outcome = ""
        if success is True:
            last_outcome = "success"
        elif success is False:
            last_outcome = "failure"

        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        """
                        SELECT success_count, failure_count
                        FROM learned_recipes
                        WHERE recipe_key = ?
                        """,
                        (normalized_key,),
                    ).fetchone()
                    success_count = int(row["success_count"]) if row is not None else 0
                    failure_count = int(row["failure_count"]) if row is not None else 0
                    if success is True:
                        success_count += 1
                    elif success is False:
                        failure_count += 1
                    conn.execute(
                        """
                        INSERT INTO learned_recipes (
                            recipe_key, recipe_text, updated_at, success_count,
                            failure_count, last_outcome, metadata
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(recipe_key) DO UPDATE SET
                            recipe_text = excluded.recipe_text,
                            updated_at = excluded.updated_at,
                            success_count = excluded.success_count,
                            failure_count = excluded.failure_count,
                            last_outcome = excluded.last_outcome,
                            metadata = excluded.metadata
                        """,
                        (
                            normalized_key,
                            normalized_recipe,
                            timestamp,
                            success_count,
                            failure_count,
                            last_outcome,
                            self._serialize_metadata(metadata),
                        ),
                    )
            except sqlite3.Error as exc:
                raise UserProfileError(f"Learned recipe update failed: {exc}") from exc

    def get_learned_recipes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._query_rows(
            """
            SELECT recipe_key, recipe_text, updated_at, success_count, failure_count, last_outcome, metadata
            FROM learned_recipes
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._normalize_limit(limit),),
        )

    def store_teach_mode_entry(
        self,
        failure_class: str,
        tool_name: str,
        correction_text: str,
        *,
        success: bool | None = None,
        metadata: Any = None,
        updated_at: str | datetime | None = None,
        record_failure_event: bool = False,
    ) -> dict[str, Any]:
        normalized_failure = self._normalize_text(failure_class, fallback="unknown", max_length=200).lower()
        normalized_tool = self._normalize_text(tool_name, fallback="unknown", max_length=120).lower()
        normalized_correction = self._normalize_text(correction_text, max_length=4000)
        if not normalized_correction:
            raise ValueError("correction_text is required")

        recipe_key = self._normalize_text(f"{normalized_failure}|{normalized_tool}", max_length=300)
        payload_metadata: dict[str, Any] = {}
        if isinstance(metadata, dict):
            payload_metadata.update(metadata)
        elif metadata is not None:
            payload_metadata["raw_metadata"] = str(metadata)
        payload_metadata.setdefault("source", "teach_mode")
        payload_metadata.setdefault("failure_class", normalized_failure)
        payload_metadata.setdefault("tool_name", normalized_tool)

        self.record_learned_recipe(
            recipe_key=recipe_key,
            recipe_text=normalized_correction,
            success=success,
            metadata=payload_metadata,
            updated_at=updated_at,
        )

        if record_failure_event:
            self.record_failure(
                failure_class=normalized_failure,
                message=f"teach-mode: {normalized_correction[:200]}",
                tool_name=normalized_tool,
                known_fix=normalized_correction,
                metadata=payload_metadata,
                event_time=updated_at,
            )

        return {
            "recipe_key": recipe_key,
            "updated_at": self._coerce_timestamp(updated_at, column_name="updated_at"),
            "last_outcome": "success" if success is True else "failure" if success is False else "",
            "record_failure_event": bool(record_failure_event),
        }

    def compact_summary(self, *, limit: int = 3) -> dict[str, Any]:
        sample = self._normalize_limit(limit, default=3, max_limit=10)
        with self._lock:
            try:
                with self._connect() as conn:
                    counts_row = conn.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM file_activity) AS file_activity,
                            (SELECT COUNT(*) FROM command_history) AS command_history,
                            (SELECT COUNT(*) FROM app_usage) AS app_usage,
                            (SELECT COUNT(*) FROM failure_memory) AS failure_memory,
                            (SELECT COUNT(*) FROM preferences) AS preferences,
                            (SELECT COUNT(*) FROM learned_recipes) AS learned_recipes
                        """
                    ).fetchone()
            except sqlite3.Error as exc:
                raise UserProfileError(f"Profile summary failed: {exc}") from exc
        counts = {key: int(counts_row[key]) for key in counts_row.keys()} if counts_row else {}
        return {
            "db_path": str(self.db_path),
            "counts": counts,
            "recent_commands": self.get_command_history(limit=sample),
            "recent_file_activity": self.get_file_activity(limit=sample),
            "recent_failures": self.get_failure_memory(limit=sample),
            "recent_apps": self.get_app_usage(limit=sample),
            "preferences": self.get_preferences(limit=sample),
            "learned_recipes": self.get_learned_recipes(limit=sample),
        }

    def compact_summary_text(self, *, limit: int = 3, max_chars: int = 700) -> str:
        summary = self.compact_summary(limit=limit)
        counts = summary.get("counts", {})
        count_text = ", ".join(f"{key}={counts.get(key, 0)}" for key in sorted(counts)) if counts else "none"
        command_items = summary.get("recent_commands", [])[:limit]
        command_text = "; ".join(item.get("command", "") for item in command_items if isinstance(item, dict))
        failure_items = summary.get("recent_failures", [])[:limit]
        failure_text = "; ".join(
            f"{item.get('failure_class', 'unknown')}:{item.get('tool_name', 'unknown')}"
            for item in failure_items
            if isinstance(item, dict)
        )
        text = (
            f"counts[{count_text}] | "
            f"commands[{command_text or 'none'}] | "
            f"failures[{failure_text or 'none'}]"
        )
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


__all__ = ["UserProfile", "UserProfileError"]
