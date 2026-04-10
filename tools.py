"""
VOCO Tools - Windows OS automation tool library.
Every tool returns:
{"status": "success" | "error" | "failure", "result": any, "message": str}
"""

from __future__ import annotations

import json
import os
import re
import difflib
import hashlib
import importlib.util
import inspect
import sqlite3
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from constants import MEMORY_DIR, WORKSPACE_PATH
from memory import SecureMemoryError, load_user_profile_dict, save_user_profile_dict

try:
    import pyautogui

    pyautogui.FAILSAFE = True
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False

try:
    import pygetwindow as gw

    PYGETWINDOW_AVAILABLE = True
except ImportError:
    PYGETWINDOW_AVAILABLE = False

try:
    from pywinauto import Desktop as PywinautoDesktop

    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False

try:
    import wmi

    WMI_AVAILABLE = True
except ImportError:
    WMI_AVAILABLE = False

try:
    import tkinter as tk

    TKINTER_AVAILABLE = True
except Exception:
    TKINTER_AVAILABLE = False

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


_playwright_instance = None
_browser_instance = None
_browser_context = None
_page_instance = None
_browser_target = "chrome"
_browser_profile_mode = "default"
_browser_effective_profile_mode = "not launched"
_browser_thread_id: int | None = None
_last_browser_launch_note = "not started"
_mute_state: bool | None = None
_FILE_INDEX_DB = MEMORY_DIR / "file_index.db"
_APP_INDEX_DB = MEMORY_DIR / "app_index.db"
_INDEX_LOCK = threading.Lock()
_system_monitor_module = None
_system_monitor_module_error = ""
_COMMON_APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "settings": "ms-settings:",
    "task manager": "taskmgr.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "vs code": "code",
    "vscode": "code",
    "chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "edge": "msedge.exe",
    "spotify": "spotify.exe",
    "powerpoint": "powerpnt.exe",
    "power point": "powerpnt.exe",
    "ppt": "powerpnt.exe",
    "pptx": "powerpnt.exe",
}

_BROWSER_STRESS_DEFAULT_SITES: tuple[str, ...] = (
    "https://www.google.com",
    "https://www.youtube.com",
    "https://www.wikipedia.org",
    "https://github.com",
    "https://stackoverflow.com",
    "https://www.microsoft.com",
    "https://www.openai.com",
    "https://www.python.org",
    "https://pypi.org",
    "https://www.reddit.com",
    "https://www.bbc.com",
    "https://www.cnn.com",
    "https://www.nytimes.com",
    "https://www.reuters.com",
    "https://weather.com",
    "https://www.imdb.com",
    "https://www.espn.com",
    "https://medium.com",
    "https://www.quora.com",
    "https://www.linkedin.com",
    "https://x.com",
    "https://www.instagram.com",
    "https://www.facebook.com",
    "https://www.amazon.com",
    "https://www.ebay.com",
    "https://www.walmart.com",
    "https://www.target.com",
    "https://www.apple.com",
    "https://www.netflix.com",
    "https://www.spotify.com",
    "https://www.dropbox.com",
    "https://www.cloudflare.com",
    "https://www.mozilla.org",
    "https://www.npmjs.com",
    "https://docs.python.org/3/",
    "https://www.w3.org",
    "https://www.kaggle.com",
    "https://arxiv.org",
    "https://www.udemy.com",
    "https://www.coursera.org",
    "https://www.khanacademy.org",
    "https://www.nasa.gov",
    "https://www.who.int",
    "https://www.un.org",
    "https://www.theguardian.com",
    "https://www.bloomberg.com",
    "https://www.canva.com",
    "https://trello.com",
    "https://www.notion.so",
    "https://duckduckgo.com",
)


def _ok(result, message: str) -> dict:
    return {"status": "success", "result": result, "message": message}


def _err(message: str) -> dict:
    return {"status": "error", "result": None, "message": message}


def _workspace_root() -> Path:
    root = WORKSPACE_PATH.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_workspace_path(path: str, allow_missing_parent: bool = False) -> Path:
    root = _workspace_root()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {path}") from exc
    if not allow_missing_parent and not resolved.exists():
        raise FileNotFoundError(f"Path not found: {resolved}")
    return resolved


def _load_system_monitor_module():
    global _system_monitor_module, _system_monitor_module_error
    if _system_monitor_module is not None:
        return _system_monitor_module
    if _system_monitor_module_error:
        return None

    module_path = Path(__file__).with_name("tools").joinpath("system_monitor.py")
    if not module_path.exists():
        _system_monitor_module_error = f"Module file missing: {module_path}"
        return None

    try:
        spec = importlib.util.spec_from_file_location("voco_system_monitor", module_path)
        if spec is None or spec.loader is None:
            _system_monitor_module_error = f"Failed to create module spec: {module_path}"
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _system_monitor_module = module
        return module
    except Exception as exc:
        _system_monitor_module_error = str(exc)
        return None


def _call_system_monitor(method_name: str, **kwargs):
    module = _load_system_monitor_module()
    if module is None:
        raise RuntimeError(f"System monitor unavailable: {_system_monitor_module_error or 'load failure'}")

    method = getattr(module, method_name, None)
    if not callable(method):
        raise RuntimeError(f"System monitor method not found: {method_name}")
    return method(**kwargs)


def _normalize_browser_target(browser: str | None) -> str:
    if not browser:
        return _browser_target
    normalized = browser.strip().lower()
    if normalized in {"chrome", "google chrome"}:
        return "chrome"
    if normalized in {"edge", "msedge", "microsoft edge"}:
        return "edge"
    if normalized in {"firefox"}:
        return "firefox"
    return "chromium"


def _normalize_browser_profile_mode(profile_mode: str | None, *, strict: bool = False) -> str:
    if profile_mode is None:
        return _browser_profile_mode
    normalized = str(profile_mode).strip().lower()
    if not normalized:
        return _browser_profile_mode

    aliases = {
        "default": "default",
        "main": "default",
        "snapshot": "snapshot",
        "copy": "snapshot",
        "automation": "automation",
        "auto": "automation",
    }
    if normalized in aliases:
        return aliases[normalized]

    if strict:
        raise ValueError(
            "Unsupported profile_mode. Use one of: default, snapshot, automation."
        )
    return _browser_profile_mode


def _context_is_closed(context) -> bool:
    if context is None:
        return True
    try:
        _ = context.pages
        return False
    except Exception:
        return True


def _close_browser_runtime() -> None:
    global _browser_instance, _browser_context, _page_instance
    try:
        if _page_instance is not None and not _page_instance.is_closed():
            _page_instance.close()
    except Exception:
        pass
    _page_instance = None

    try:
        if _browser_context is not None and not _context_is_closed(_browser_context):
            _browser_context.close()
    except Exception:
        pass
    _browser_context = None

    try:
        if _browser_instance is not None and _browser_instance.is_connected():
            _browser_instance.close()
    except Exception:
        pass
    _browser_instance = None


def _stop_playwright_runtime() -> None:
    global _playwright_instance
    if _playwright_instance is None:
        return
    try:
        _playwright_instance.stop()
    except Exception:
        pass
    _playwright_instance = None


def _reset_browser_runtime(full_reset: bool = False) -> None:
    _close_browser_runtime()
    if full_reset:
        _stop_playwright_runtime()


def _get_default_profile_config(browser_target: str) -> tuple[str, str] | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None

    if browser_target == "chrome":
        return os.path.join(local_app_data, "Google", "Chrome", "User Data"), "chrome"
    if browser_target == "edge":
        return os.path.join(local_app_data, "Microsoft", "Edge", "User Data"), "msedge"
    return None


def _short_browser_launch_error(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    if "Target page, context or browser has been closed" in message:
        return "profile is locked or already in use by another Chrome session"
    return message[:180] if message else "unknown launch error"


def _prepare_profile_snapshot(user_data_dir: str, browser_target: str) -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None

    snapshot_root = os.path.join(
        local_app_data,
        "VOCO",
        "playwright-profiles",
        f"{browser_target}-default-snapshot",
    )
    try:
        if os.path.isdir(snapshot_root):
            shutil.rmtree(snapshot_root, ignore_errors=True)
        os.makedirs(snapshot_root, exist_ok=True)
    except Exception:
        return None

    copy_targets = ["Default", "Local State"]
    ignore_entries = shutil.ignore_patterns(
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnCache",
        "Crashpad",
        "Service Worker",
        "optimization_guide_model_store",
        "GraphiteDawnCache",
        "Media Cache",
    )

    for item in copy_targets:
        src = os.path.join(user_data_dir, item)
        dst = os.path.join(snapshot_root, item)
        if not os.path.exists(src):
            continue
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore_entries)
            else:
                shutil.copy2(src, dst)
        except Exception:
            continue
    return snapshot_root


def _launch_playwright_context(browser_target: str, profile_mode: str):
    global _last_browser_launch_note, _browser_effective_profile_mode
    launch_args = ["--start-maximized", "--disable-blink-features=AutomationControlled"]
    requested_mode = _normalize_browser_profile_mode(profile_mode)
    _browser_effective_profile_mode = requested_mode
    _last_browser_launch_note = f"launching {browser_target} with {requested_mode} profile mode"

    profile_config = _get_default_profile_config(browser_target)
    if profile_config is not None and requested_mode in {"default", "snapshot"}:
        user_data_dir, channel = profile_config
        has_default_profile = os.path.isdir(user_data_dir)

        if requested_mode == "default":
            if has_default_profile:
                try:
                    context = _playwright_instance.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        channel=channel,
                        headless=False,
                        args=launch_args + ["--profile-directory=Default"],
                        no_viewport=True,
                    )
                    _browser_effective_profile_mode = "default"
                    _last_browser_launch_note = f"using default {browser_target} profile"
                    return None, context
                except Exception as exc:
                    reason = _short_browser_launch_error(exc)
                    snapshot_dir = _prepare_profile_snapshot(
                        user_data_dir=user_data_dir,
                        browser_target=browser_target,
                    )
                    if snapshot_dir is not None:
                        try:
                            context = _playwright_instance.chromium.launch_persistent_context(
                                user_data_dir=snapshot_dir,
                                channel=channel,
                                headless=False,
                                args=launch_args + ["--profile-directory=Default"],
                                no_viewport=True,
                            )
                            _browser_effective_profile_mode = "snapshot"
                            _last_browser_launch_note = (
                                f"default {browser_target} profile locked; using profile snapshot"
                            )
                            return None, context
                        except Exception as snapshot_exc:
                            snapshot_reason = _short_browser_launch_error(snapshot_exc)
                            _last_browser_launch_note = (
                                f"default {browser_target} profile unavailable ({reason}); "
                                f"snapshot fallback failed ({snapshot_reason}); using automation profile"
                            )
                    else:
                        _last_browser_launch_note = (
                            f"default {browser_target} profile unavailable ({reason}); using automation profile"
                        )
            else:
                _last_browser_launch_note = (
                    f"default {browser_target} profile path not found; using automation profile"
                )
        elif requested_mode == "snapshot":
            if has_default_profile:
                snapshot_dir = _prepare_profile_snapshot(
                    user_data_dir=user_data_dir,
                    browser_target=browser_target,
                )
                if snapshot_dir is not None:
                    try:
                        context = _playwright_instance.chromium.launch_persistent_context(
                            user_data_dir=snapshot_dir,
                            channel=channel,
                            headless=False,
                            args=launch_args + ["--profile-directory=Default"],
                            no_viewport=True,
                        )
                        _browser_effective_profile_mode = "snapshot"
                        _last_browser_launch_note = (
                            f"using snapshot of default {browser_target} profile"
                        )
                        return None, context
                    except Exception as exc:
                        reason = _short_browser_launch_error(exc)
                        _last_browser_launch_note = (
                            f"snapshot {browser_target} profile unavailable ({reason}); using automation profile"
                        )
                else:
                    _last_browser_launch_note = (
                        f"snapshot {browser_target} profile unavailable (snapshot preparation failed); "
                        "using automation profile"
                    )
            else:
                _last_browser_launch_note = (
                    f"snapshot source profile not found for {browser_target}; using automation profile"
                )

    if browser_target == "firefox":
        browser = _playwright_instance.firefox.launch(headless=False)
        context = browser.new_context(no_viewport=True)
        _browser_effective_profile_mode = "automation"
        if requested_mode == "automation":
            _last_browser_launch_note = "using firefox automation profile"
        else:
            _last_browser_launch_note = (
                f"{requested_mode} profile mode is not supported for firefox; using automation profile"
            )
        return browser, context

    launch_kwargs = {"headless": False, "args": launch_args}
    if browser_target == "chrome":
        launch_kwargs["channel"] = "chrome"
    elif browser_target == "edge":
        launch_kwargs["channel"] = "msedge"
    browser = _playwright_instance.chromium.launch(**launch_kwargs)
    context = browser.new_context(no_viewport=True)
    _browser_effective_profile_mode = "automation"
    if requested_mode == "automation":
        _last_browser_launch_note = f"using {browser_target} automation profile"
    elif "using automation profile" not in _last_browser_launch_note:
        _last_browser_launch_note = (
            f"{requested_mode} {browser_target} profile unavailable; using automation profile"
        )
    return browser, context


def _acquire_browser_page():
    global _page_instance
    if _browser_context is None:
        return None
    if _page_instance is not None and not _page_instance.is_closed():
        return _page_instance

    try:
        pages = _browser_context.pages
    except Exception:
        return None
    if pages:
        _page_instance = pages[0]
    else:
        try:
            _page_instance = _browser_context.new_page()
        except Exception:
            return None
    return _page_instance


def _get_browser_page(browser: str | None = None, profile_mode: str | None = None):
    global _playwright_instance, _browser_instance, _browser_context, _page_instance
    global _browser_target, _browser_profile_mode, _browser_effective_profile_mode
    global _browser_thread_id, _last_browser_launch_note
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        )

    current_thread_id = threading.get_ident()
    if _browser_thread_id is not None and _browser_thread_id != current_thread_id:
        # Reinitialize browser runtime when command execution thread changes.
        _reset_browser_runtime(full_reset=True)
        _browser_target = "chrome"
        _browser_profile_mode = "default"
        _browser_effective_profile_mode = "not launched"
        _last_browser_launch_note = "thread reset; profile launch pending"
    if _browser_thread_id is None or _browser_thread_id != current_thread_id:
        _browser_thread_id = current_thread_id

    requested_target = _normalize_browser_target(browser)
    requested_profile_mode = _normalize_browser_profile_mode(profile_mode)
    browser_needs_restart = browser is not None and requested_target != _browser_target
    profile_needs_restart = profile_mode is not None and requested_profile_mode != _browser_profile_mode

    if _playwright_instance is None:
        _playwright_instance = sync_playwright().start()

    if browser_needs_restart or profile_needs_restart:
        _close_browser_runtime()
        _last_browser_launch_note = (
            f"session reset for browser={requested_target}, profile_mode={requested_profile_mode}"
        )

    if _browser_context is None or _context_is_closed(_browser_context):
        _browser_instance, _browser_context = _launch_playwright_context(
            requested_target,
            requested_profile_mode,
        )
        _browser_target = requested_target
        _browser_profile_mode = requested_profile_mode

    page = _acquire_browser_page()
    if page is None:
        _close_browser_runtime()
        _browser_instance, _browser_context = _launch_playwright_context(
            requested_target,
            requested_profile_mode,
        )
        _browser_target = requested_target
        _browser_profile_mode = requested_profile_mode
        page = _acquire_browser_page()
        if page is None:
            raise RuntimeError("Failed to initialize browser page context.")
    return page


_INTERACTIVE_BROWSER_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "listitem",
    "menuitem",
    "tab",
    "checkbox",
    "radio",
    "option",
    "switch",
    "slider",
    "spinbutton",
}


def _flatten_accessibility_tree(node: dict | None, result: list[dict]) -> None:
    if not node:
        return
    role = str(node.get("role", "")).strip().lower()
    name = str(node.get("name", "")).strip()
    value = str(node.get("value", "")).strip()
    if role in _INTERACTIVE_BROWSER_ROLES:
        if name or value:
            result.append({"role": role, "name": name[:140], "value": value[:140]})
    for child in node.get("children", []) or []:
        _flatten_accessibility_tree(child, result)


def _snapshot_browser_state(page, max_elements: int = 60) -> dict:
    title = ""
    try:
        title = page.title()
    except Exception:
        title = ""

    try:
        snapshot = page.accessibility.snapshot()
    except Exception:
        snapshot = None

    elements: list[dict] = []
    _flatten_accessibility_tree(snapshot, elements)
    return {
        "url": page.url,
        "title": title,
        "elements": elements[: max(10, int(max_elements))],
        "element_count": len(elements),
        "browser": _browser_target,
        "profile_mode": _browser_profile_mode,
        "effective_profile_mode": _browser_effective_profile_mode,
        "launch_mode": _last_browser_launch_note,
    }


def _normalize_browser_url_candidate(candidate: str) -> str | None:
    cleaned = str(candidate).strip().strip("\"'").rstrip(".,;:!?)")
    if not cleaned:
        return None
    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned
    tld = cleaned.rsplit(".", 1)[-1].lower()
    if tld in {"exe", "msi", "bat", "cmd", "ps1", "lnk"}:
        return None
    return f"https://{cleaned}"


def _normalize_navigation_url(url: str) -> str:
    candidate = str(url).strip()
    normalized = _normalize_browser_url_candidate(candidate)
    return normalized or candidate


def _coerce_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def _set_clipboard_text(value: str) -> tuple[bool, str]:
    if not TKINTER_AVAILABLE:
        return False, "tkinter is not available for clipboard operations."
    try:
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(str(value))
        root.update()
        root.destroy()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _get_clipboard_text() -> tuple[str | None, str]:
    if not TKINTER_AVAILABLE:
        return None, "tkinter is not available for clipboard operations."
    try:
        root = tk.Tk()
        root.withdraw()
        content = root.clipboard_get()
        root.destroy()
        return str(content), ""
    except Exception as exc:
        return None, str(exc)


def _split_site_candidates(sites: str | None) -> list[str]:
    raw = str(sites or "").strip()
    if not raw:
        return []
    candidates = re.split(r"[\r\n,;]+", raw)
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = _normalize_browser_url_candidate(candidate)
        if resolved is None:
            continue
        if resolved in seen:
            continue
        normalized.append(resolved)
        seen.add(resolved)
    return normalized


def _build_browser_stress_site_list(sites: str | None, site_count: int) -> list[str]:
    bounded_count = _coerce_int(site_count, default=50, minimum=1, maximum=200)
    provided = _split_site_candidates(sites)
    if provided:
        return provided[:bounded_count]
    return list(_BROWSER_STRESS_DEFAULT_SITES[:bounded_count])


def _desktop_root() -> Path:
    user_profile = str(os.environ.get("USERPROFILE", "")).strip()
    if not user_profile:
        raise RuntimeError("USERPROFILE is not configured; Desktop path is unavailable.")
    desktop = Path(user_profile) / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    return desktop


def _sanitize_desktop_filename(filename: str | None, default_name: str) -> str:
    candidate = Path(str(filename or "").strip()).name
    if not candidate:
        candidate = default_name
    if not candidate.lower().endswith(".txt"):
        candidate = f"{candidate}.txt"
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", candidate).strip(" .")
    if not cleaned:
        cleaned = default_name
    if not cleaned.lower().endswith(".txt"):
        cleaned = f"{cleaned}.txt"
    if len(cleaned) > 120:
        cleaned = cleaned[:116] + ".txt"
    return cleaned


def _load_user_profile_dict() -> dict:
    try:
        loaded = load_user_profile_dict()
    except SecureMemoryError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


_SENSITIVE_PROFILE_KEY_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "auth",
    "cookie",
    "session",
    "private",
)


def _is_sensitive_profile_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    return any(token in normalized for token in _SENSITIVE_PROFILE_KEY_TOKENS)


def _format_profile_value_for_display(value: object, *, sensitive: bool) -> str:
    if sensitive:
        return "[REDACTED]"
    text = str(value)
    if len(text) <= 120:
        return text
    return text[:117] + "..."


def _extract_python_code(raw_text: str) -> str | None:
    text = str(raw_text or "").strip()
    if not text:
        return None

    fenced_patterns = [
        r"```python\s*(.*?)```",
        r"```py\s*(.*?)```",
        r"```\s*(.*?)```",
    ]
    for pattern in fenced_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        for match in matches:
            candidate = str(match).strip()
            if not candidate:
                continue
            if any(token in candidate for token in ("def ", "class ", "import ", "print(", "if __name__")):
                return candidate

    if any(token in text for token in ("def ", "class ", "import ", "print(", "if __name__")):
        return text
    return None


def _render_codegen_command(
    template: str,
    *,
    prompt: str,
    filename: str,
    previous_code: str | None = None,
    error_text: str | None = None,
) -> str:
    command = str(template or "").strip()
    if not command:
        return ""

    replacements = {
        "{prompt}": prompt,
        "{filename}": filename,
        "{code}": previous_code or "",
        "{error}": error_text or "",
    }
    used_placeholder = any(token in command for token in replacements)
    for token, value in replacements.items():
        command = command.replace(token, value)

    if not used_placeholder:
        escaped_prompt = prompt.replace("'", "''")
        command = f"{command} '{escaped_prompt}'"
    return command


def _run_configured_codegen_command(
    command_template: str,
    *,
    prompt: str,
    filename: str,
    previous_code: str | None = None,
    error_text: str | None = None,
) -> tuple[bool, str]:
    command = _render_codegen_command(
        command_template,
        prompt=prompt,
        filename=filename,
        previous_code=previous_code,
        error_text=error_text,
    )
    if not command:
        return False, "Configured web assistant command is empty."

    try:
        proc = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return False, "Configured web assistant command timed out after 45 seconds."
    except Exception as exc:
        return False, f"Configured web assistant command failed: {exc}"

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        detail = output[:400] if output else "no output"
        return False, f"Configured web assistant command exited with {proc.returncode}: {detail}"
    code = _extract_python_code(output)
    if not code:
        preview = output[:280] if output else "empty response"
        return False, f"Configured web assistant returned no Python code. Output preview: {preview}"
    return True, code


def _run_free_ai_codegen(prompt: str) -> tuple[bool, str]:
    try:
        from llm import check_ollama_running, generate_with_history
    except Exception as exc:
        return False, f"Free AI path unavailable: {exc}"

    if not check_ollama_running():
        return False, "Free AI path unavailable: Ollama is not running with the configured model."

    system_prompt = (
        "You are a Python coding assistant.\n"
        "Return only runnable Python code.\n"
        "Do not include markdown fences or explanations.\n"
    )
    response = generate_with_history(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.05,
    )
    response_text = str(response or "").strip()
    if not response_text:
        return False, "Free AI path returned an empty response."

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict) and first.get("tool") == "report_failure":
            reason = str((first.get("args") or {}).get("reason") or first.get("reason") or "").strip()
            return False, f"Free AI path returned failure plan: {reason or 'unknown reason'}"

    code = _extract_python_code(response_text)
    if not code:
        preview = response_text[:280]
        return False, f"Free AI path returned no Python code. Output preview: {preview}"
    return True, code


def _run_chatgpt_codegen(prompt: str, timeout_seconds: int = 120) -> tuple[bool, str]:
    bounded_timeout = _coerce_int(timeout_seconds, default=120, minimum=30, maximum=240)
    request_text = (
        "Generate one complete runnable Python script for this task.\n"
        "Return only Python code inside a single ```python``` block.\n"
        "The script must run non-interactively without requiring stdin input.\n"
        "Do not include any explanation text.\n\n"
        f"Task:\n{prompt}\n"
    )

    try:
        page = _get_browser_page(browser="chrome", profile_mode="default")
        page.bring_to_front()
        page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
    except Exception as exc:
        return False, f"ChatGPT browser path unavailable: {exc}"

    input_selector = ""
    input_candidates = [
        "textarea#prompt-textarea",
        "textarea",
        "div[role='textbox'][contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
    ]
    target_locator = None
    for selector in input_candidates:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=6000)
            locator.click()
            target_locator = locator
            input_selector = selector
            break
        except Exception:
            continue

    if target_locator is None and _focus_best_text_input(page):
        input_selector = "focused-fallback"
    elif target_locator is None:
        return False, "ChatGPT input is not available. Sign in to chatgpt.com in Chrome default profile first."

    try:
        page.keyboard.press("Control+A")
        page.keyboard.type(request_text)
        page.keyboard.press("Enter")
    except Exception as exc:
        return False, f"Failed to submit prompt to ChatGPT via '{input_selector}': {exc}"

    deadline = time.time() + bounded_timeout
    clarification_sent = False
    while time.time() < deadline:
        try:
            snippets: list[str] = []
            for node in page.query_selector_all("article pre code, main pre code, pre code"):
                try:
                    text = node.inner_text().strip()
                except Exception:
                    continue
                if text:
                    snippets.append(text)
            if snippets:
                best = max(snippets, key=len)
                extracted = _extract_python_code(best) or best
                extracted = extracted.strip()
                if extracted:
                    return True, extracted
        except Exception:
            pass

        try:
            main_text = page.inner_text("main")[:50000]
        except Exception:
            main_text = ""
        fenced_matches = re.findall(
            r"```(?:python|py)\s*(.*?)```",
            main_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced_matches:
            best_fenced = max((match.strip() for match in fenced_matches if str(match).strip()), key=len, default="")
            if best_fenced:
                return True, best_fenced

        if not clarification_sent:
            lower_main = main_text.lower()
            clarification_markers = [
                "what task should",
                "please describe",
                "what should the script",
                "share more details",
            ]
            if any(marker in lower_main for marker in clarification_markers):
                follow_up = (
                    f"Task: {prompt}\n"
                    "Return one complete runnable Python script only in a ```python``` block. "
                    "No explanation."
                )
                try:
                    sent = False
                    for selector in input_candidates:
                        try:
                            locator = page.locator(selector).first
                            locator.wait_for(state="visible", timeout=2000)
                            locator.click()
                            sent = True
                            break
                        except Exception:
                            continue
                    if not sent:
                        sent = _focus_best_text_input(page)
                    if sent:
                        page.keyboard.press("Control+A")
                        page.keyboard.type(follow_up)
                        page.keyboard.press("Enter")
                        clarification_sent = True
                except Exception:
                    pass

        page.wait_for_timeout(1200)

    preview = ""
    try:
        preview = re.sub(r"\s+", " ", page.inner_text("main"))[:280]
    except Exception:
        preview = "no readable response"
    return False, f"ChatGPT returned no Python code within {bounded_timeout}s. Preview: {preview}"


def _request_codegen_candidate(
    *,
    prompt: str,
    filename: str,
    previous_code: str | None = None,
    error_text: str | None = None,
) -> tuple[bool, str, str, list[str]]:
    diagnostics: list[str] = []
    profile = _load_user_profile_dict()
    prompt_text = str(prompt or "")
    prefer_chatgpt = _coerce_bool(profile.get("prefer_chatgpt_codegen"), default=False) or _coerce_bool(
        os.environ.get("VOCO_USE_CHATGPT_CODEGEN"),
        default=False,
    )
    explicit_chatgpt_request = re.search(r"\b(chatgpt|chat\s*gpt|openai)\b", prompt_text, flags=re.IGNORECASE) is not None

    if explicit_chatgpt_request or prefer_chatgpt:
        ok, payload = _run_chatgpt_codegen(prompt=prompt_text)
        if ok:
            return True, payload, "chatgpt-browser", diagnostics
        diagnostics.append(payload)

    configured_command = str(
        profile.get("web_assistant_command") or os.environ.get("VOCO_WEB_ASSISTANT_COMMAND") or ""
    ).strip()
    if configured_command:
        ok, payload = _run_configured_codegen_command(
            configured_command,
            prompt=prompt,
            filename=filename,
            previous_code=previous_code,
            error_text=error_text,
        )
        if ok:
            return True, payload, "configured-web-assistant", diagnostics
        diagnostics.append(payload)

    ok, payload = _run_free_ai_codegen(prompt)
    if ok:
        return True, payload, "free-ai-ollama", diagnostics
    diagnostics.append(payload)
    return False, "", "none", diagnostics


def _run_python_file(path: Path, timeout_seconds: int) -> dict:
    bounded_timeout = _coerce_int(timeout_seconds, default=20, minimum=5, maximum=120)
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            ["python", str(path.name)],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=bounded_timeout,
        )
        elapsed = round(time.perf_counter() - start, 3)
        return {
            "timed_out": False,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[:4000],
            "stderr": (proc.stderr or "")[:4000],
            "elapsed_seconds": elapsed,
            "timeout_seconds": bounded_timeout,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.perf_counter() - start, 3)
        return {
            "timed_out": True,
            "returncode": None,
            "stdout": (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[:4000] if isinstance(exc.stderr, str) else "",
            "elapsed_seconds": elapsed,
            "timeout_seconds": bounded_timeout,
        }


def _summarize_python_failure(run_result: dict) -> str:
    if run_result.get("timed_out"):
        return f"Execution timed out after {run_result.get('timeout_seconds')}s."

    stderr = str(run_result.get("stderr", "")).strip()
    stdout = str(run_result.get("stdout", "")).strip()
    combined = stderr or stdout
    if not combined:
        return f"Python exited with return code {run_result.get('returncode')} and no diagnostics."
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return f"Python exited with return code {run_result.get('returncode')}."
    return lines[-1][:260]


_ELEMENT_NOISE_WORDS = {
    "on",
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "please",
    "kindly",
    "to",
    "for",
    "of",
    "click",
    "open",
    "play",
    "select",
}
_GENERIC_CLICK_TRIGGER_WORDS = {
    "video",
    "result",
    "item",
    "link",
    "article",
    "post",
    "entry",
}
_FUZZY_CLICK_MIN_SCORE = 0.58


def _strip_element_noise(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return ""
    tokens = re.findall(r"[a-z0-9]+", normalized)
    filtered = [token for token in tokens if token not in _ELEMENT_NOISE_WORDS]
    if filtered:
        return " ".join(filtered).strip()
    return normalized


def _resolve_ordinal(text: str, default: int = 1) -> tuple[str, int]:
    cleaned = _strip_element_noise(str(text).strip())
    if not cleaned:
        return "", max(1, int(default))

    ordinals = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
    for word, value in ordinals.items():
        if re.search(rf"\b{word}\b", cleaned, flags=re.IGNORECASE):
            cleaned = re.sub(rf"\b(?:the\s+)?{word}\b", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = _strip_element_noise(cleaned)
            return cleaned, value

    numeric = re.search(r"\b(\d+)(?:st|nd|rd|th)?\b", cleaned, flags=re.IGNORECASE)
    if numeric:
        value = max(1, int(numeric.group(1)))
        cleaned = re.sub(r"\b\d+(?:st|nd|rd|th)?\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = _strip_element_noise(cleaned)
        return cleaned, value

    return _strip_element_noise(cleaned), max(1, int(default))


def _browser_runtime_state() -> dict:
    has_context = _browser_context is not None and not _context_is_closed(_browser_context)
    try:
        has_page = _page_instance is not None and not _page_instance.is_closed()
    except Exception:
        has_page = False
    return {
        "browser": _browser_target,
        "profile_mode": _browser_profile_mode,
        "effective_profile_mode": _browser_effective_profile_mode,
        "launch_mode": _last_browser_launch_note,
        "context_active": has_context,
        "page_active": has_page,
    }


def browser_switch_profile(
    profile_mode: str | None = None,
    browser: str | None = None,
    relaunch: bool = True,
) -> dict:
    global _browser_target, _browser_profile_mode, _browser_effective_profile_mode, _last_browser_launch_note

    try:
        requested_mode = _normalize_browser_profile_mode(
            profile_mode,
            strict=profile_mode is not None,
        )
    except ValueError as exc:
        return _err(str(exc))

    requested_target = _normalize_browser_target(browser)
    mode_changed = requested_mode != _browser_profile_mode
    target_changed = requested_target != _browser_target

    if mode_changed or target_changed:
        _reset_browser_runtime(full_reset=True)

    _browser_target = requested_target
    _browser_profile_mode = requested_mode
    _browser_effective_profile_mode = "not launched"
    _last_browser_launch_note = (
        f"profile switch configured: browser={requested_target}, profile_mode={requested_mode}; launch pending"
    )

    if not relaunch:
        state = _browser_runtime_state()
        return _ok(
            state,
            f"Configured browser={requested_target}, profile_mode={requested_mode}. Relaunch pending.",
        )

    try:
        page = _get_browser_page(browser=requested_target, profile_mode=requested_mode)
        state = _snapshot_browser_state(page)
        return _ok(
            state,
            (
                "Switched browser session to "
                f"{state['browser']} with {state['profile_mode']} mode "
                f"(effective: {state['effective_profile_mode']}; {state['launch_mode']})."
            ),
        )
    except Exception as exc:
        return _err(f"Profile switch failed: {exc}")


def _extract_browser_text_lines(page, text_query: str | None = None, text_limit: int = 20) -> list[str]:
    bounded_limit = _coerce_int(text_limit, default=20, minimum=1, maximum=120)
    query_tokens = [token for token in re.findall(r"[a-z0-9]+", str(text_query or "").lower()) if token]

    raw_text = ""
    try:
        raw_text = page.inner_text("body")
    except Exception:
        raw_text = ""

    if not raw_text.strip():
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for line in raw_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if len(normalized) < 6:
            continue
        lowered = normalized.lower()
        if query_tokens and not any(token in lowered for token in query_tokens):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
        if len(deduped) >= bounded_limit:
            break

    if deduped or not query_tokens:
        return deduped

    for line in raw_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if len(normalized) < 6:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
        if len(deduped) >= bounded_limit:
            break
    return deduped


def browser_get_state(
    max_elements: int = 60,
    text_query: str | None = None,
    text_limit: int = 20,
    copy_to_clipboard: bool = False,
) -> dict:
    try:
        page = _get_browser_page()
        state = _snapshot_browser_state(page, max_elements=max_elements)
        extracted_lines = _extract_browser_text_lines(page=page, text_query=text_query, text_limit=text_limit)
        if extracted_lines:
            state["text_preview"] = extracted_lines[: min(5, len(extracted_lines))]
            state["text_line_count"] = len(extracted_lines)

        if bool(copy_to_clipboard):
            if not extracted_lines:
                return _err("No readable page text available to copy from browser state.")
            clipboard_text = "\n".join(extracted_lines)
            copied, error = _set_clipboard_text(clipboard_text)
            if not copied:
                return _err(f"Captured browser state but failed copying text to clipboard: {error}")
            state["clipboard_copied"] = True
            state["clipboard_characters"] = len(clipboard_text)
            return _ok(
                state,
                (
                    f"Captured browser state from {state['url']} with {state['element_count']} interactive elements "
                    f"and copied {len(extracted_lines)} text lines to clipboard."
                ),
            )
        return _ok(
            state,
            f"Captured browser state from {state['url']} with {state['element_count']} interactive elements.",
        )
    except Exception as exc:
        return _err(f"Failed to read browser state: {exc}")


def get_page_title() -> dict:
    try:
        page = _get_browser_page()
        title = str(page.title() or "").strip()
        if not title:
            return _err("Active browser page title is empty.")
        payload = {"title": title, "url": page.url}
        return _ok(payload, f"Read active browser page title: {title}")
    except Exception as exc:
        return _err(f"Failed to read browser page title: {exc}")


def copy_text_to_clipboard(text: str) -> dict:
    value = str(text or "")
    if not value.strip():
        return _err("No text provided to copy to clipboard.")
    copied, error = _set_clipboard_text(value)
    if not copied:
        return _err(f"Failed to copy text to clipboard: {error}")
    payload = {"text": value, "characters": len(value)}
    return _ok(payload, f"Copied {len(value)} characters to clipboard.")


def browser_navigate(url: str, browser: str | None = None) -> dict:
    target_url = _normalize_navigation_url(url)
    try:
        page = _get_browser_page(browser=browser)
        page.bring_to_front()
        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        state = _snapshot_browser_state(page)
        return _ok(state, f"Navigated browser to {state['url']}.")
    except PlaywrightTimeoutError:
        return _err(f"Browser navigation timed out for URL: {target_url}")
    except Exception as exc:
        return _err(f"Browser navigation failed: {exc}")


def _normalize_newline_mode(newline_mode: str | None, multiline: bool | None, typed: str) -> str:
    normalized_mode = str(newline_mode or "").strip().lower().replace("-", "_").replace("+", "_")
    if normalized_mode in {"shift_enter", "shiftenter", "safe", "multiline"}:
        return "shift_enter"
    if normalized_mode in {"enter", "submit"}:
        return "enter"
    if bool(multiline) or "\n" in typed:
        return "shift_enter"
    return "literal"


def _type_text_with_newline_policy(page, text: str, newline_mode: str) -> None:
    lines = str(text).split("\n")
    mode = _normalize_newline_mode(newline_mode=newline_mode, multiline=True, typed=str(text))
    for index, line in enumerate(lines):
        if line:
            page.keyboard.type(line)
        if index >= len(lines) - 1:
            continue
        if mode == "shift_enter":
            page.keyboard.press("Shift+Enter")
        else:
            page.keyboard.press("Enter")


def browser_type(
    text: str,
    element_name: str | None = None,
    clear: bool = True,
    multiline: bool | None = None,
    newline_mode: str | None = None,
    submit: bool = False,
) -> dict:
    typed = str(text)
    if not typed:
        return _err("No text provided for browser typing.")

    target_name = str(element_name or "").strip()
    try:
        page = _get_browser_page()
        page.bring_to_front()
        target = None

        if target_name:
            escaped = re.escape(target_name)
            for role in ["searchbox", "textbox", "combobox"]:
                try:
                    locator = page.get_by_role(role, name=re.compile(escaped, re.IGNORECASE))
                    if locator.count() > 0:
                        target = locator.first
                        break
                except Exception:
                    continue
            if target is None:
                try:
                    locator = page.get_by_label(re.compile(escaped, re.IGNORECASE))
                    if locator.count() > 0:
                        target = locator.first
                except Exception:
                    target = None

        if target is not None:
            target.click(timeout=5000)
        else:
            if not _focus_best_text_input(page):
                return _err("No editable browser field found to type into.")

        if clear:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")

        normalized_multiline = bool(multiline) or ("\n" in typed)
        resolved_newline_mode = _normalize_newline_mode(
            newline_mode=newline_mode,
            multiline=multiline,
            typed=typed,
        )
        if normalized_multiline:
            _type_text_with_newline_policy(page, typed, resolved_newline_mode)
        else:
            page.keyboard.type(typed)

        if bool(submit):
            page.keyboard.press("Enter")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass

        state = _snapshot_browser_state(page)
        destination = f" into '{target_name}'" if target_name else ""
        if normalized_multiline and not bool(submit):
            return _ok(state, f"Typed multiline text{destination} in browser using {resolved_newline_mode}.")
        if bool(submit):
            return _ok(state, f"Typed '{typed}'{destination} and submitted in browser.")
        return _ok(state, f"Typed '{typed}'{destination} in browser.")
    except PlaywrightTimeoutError:
        return _err("Timed out while typing in browser.")
    except Exception as exc:
        return _err(f"Browser typing failed: {exc}")


def browser_press_key(key: str) -> dict:
    key_name = str(key).strip() or "Enter"
    try:
        page = _get_browser_page()
        page.bring_to_front()
        page.keyboard.press(key_name)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        state = _snapshot_browser_state(page)
        return _ok(state, f"Pressed browser key: {key_name}")
    except Exception as exc:
        return _err(f"Browser key press failed: {exc}")


def browser_click(element_name: str, role: str | None = None, occurrence: int = 1) -> dict:
    raw_name = str(element_name or "").strip()
    if not raw_name:
        return _err("Element name is required for browser click.")

    cleaned_name, resolved_occurrence = _resolve_ordinal(raw_name, default=occurrence)
    cleaned_name = _strip_element_noise(cleaned_name)
    occurrence_index = max(0, resolved_occurrence - 1)
    role_name = str(role or "").strip().lower() or None

    try:
        page = _get_browser_page()
        page.bring_to_front()

        if role_name:
            role_candidates = [role_name]
        else:
            role_candidates = ["button", "link", "menuitem", "tab", "option", "listitem"]

        target_locator = None
        if cleaned_name:
            escaped = re.escape(cleaned_name)
            for candidate_role in role_candidates:
                try:
                    locator = page.get_by_role(candidate_role, name=re.compile(escaped, re.IGNORECASE))
                    if locator.count() > occurrence_index:
                        target_locator = locator.nth(occurrence_index)
                        break
                except Exception:
                    continue

            if target_locator is None:
                try:
                    text_locator = page.get_by_text(re.compile(escaped, re.IGNORECASE))
                    if text_locator.count() > occurrence_index:
                        target_locator = text_locator.nth(occurrence_index)
                except Exception:
                    target_locator = None

        cleaned_tokens = set(re.findall(r"[a-z0-9]+", cleaned_name.lower()))
        raw_tokens = set(re.findall(r"[a-z0-9]+", raw_name.lower()))
        fallback_trigger = (
            not cleaned_name
            or bool(cleaned_tokens & _GENERIC_CLICK_TRIGGER_WORDS)
            or bool(raw_tokens & _GENERIC_CLICK_TRIGGER_WORDS)
        )
        if target_locator is None and fallback_trigger:
            fallback_selectors = [
                "ytd-video-renderer #video-title",
                "a#video-title",
                "a[href*='watch']",
                "main a",
                "article a",
            ]
            for selector in fallback_selectors:
                try:
                    locator = page.locator(selector)
                    if locator.count() > occurrence_index:
                        target_locator = locator.nth(occurrence_index)
                        break
                except Exception:
                    continue

        if target_locator is None and cleaned_name:
            try:
                candidate_locator = page.locator("a, button, [role='link'], [role='button']")
                candidate_count = min(candidate_locator.count(), 120)
                scored_candidates: list[tuple[float, object]] = []
                lowered_target = cleaned_name.lower()
                for idx in range(candidate_count):
                    node = candidate_locator.nth(idx)
                    try:
                        label = ""
                        try:
                            label = str(node.inner_text(timeout=300) or "").strip()
                        except Exception:
                            label = ""
                        if not label:
                            for attr in ("aria-label", "title"):
                                attr_value = str(node.get_attribute(attr) or "").strip()
                                if attr_value:
                                    label = attr_value
                                    break
                        if not label:
                            continue
                        score = difflib.SequenceMatcher(None, lowered_target, label.lower()).ratio()
                        scored_candidates.append((score, node))
                    except Exception:
                        continue

                scored_candidates.sort(key=lambda item: item[0], reverse=True)
                if len(scored_candidates) > occurrence_index:
                    best_score, best_node = scored_candidates[occurrence_index]
                    if best_score >= _FUZZY_CLICK_MIN_SCORE:
                        target_locator = best_node
            except Exception:
                target_locator = None

        if target_locator is None:
            return _err(f"No browser element matched '{raw_name}'.")

        target_locator.click(timeout=8000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        state = _snapshot_browser_state(page)
        return _ok(state, f"Clicked browser element '{raw_name}'.")
    except PlaywrightTimeoutError:
        return _err(f"Timed out while clicking '{raw_name}' in browser.")
    except Exception as exc:
        return _err(f"Browser click failed: {exc}")


def open_browser(url: str = "https://www.google.com", browser: str = "chromium") -> dict:
    return browser_navigate(url=url, browser=browser)


def navigate_to(url: str) -> dict:
    return browser_navigate(url=url, browser=None)


def search_youtube(query: str) -> dict:
    try:
        page = _get_browser_page()
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector("ytd-video-renderer #video-title", timeout=10000)
        first_result = page.query_selector("ytd-video-renderer #video-title")
        if first_result is None:
            return _err("No YouTube results found.")
        title = first_result.inner_text().strip()
        href = first_result.get_attribute("href") or ""
        full_url = f"https://www.youtube.com{href}" if href.startswith("/") else href
        return _ok({"title": title, "url": full_url}, f"Found top YouTube result: {title}")
    except PlaywrightTimeoutError:
        return _err("YouTube results did not load in time.")
    except Exception as exc:
        return _err(f"YouTube search failed: {exc}")


def click_youtube_first_result() -> dict:
    try:
        page = _get_browser_page()
        first_link = page.query_selector("ytd-video-renderer #video-title")
        if first_link is None:
            return _err("No clickable YouTube result found.")
        first_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        return _ok(
            {"url": page.url, "title": page.title()},
            f"Opened first YouTube result: {page.title()}",
        )
    except PlaywrightTimeoutError:
        return _err("Timed out while opening the first YouTube result.")
    except Exception as exc:
        return _err(f"Click failed: {exc}")


def web_search(query: str, engine: str = "google") -> dict:
    engine_normalized = engine.strip().lower()
    if engine_normalized not in {"google", "bing"}:
        return _err("Unsupported engine. Use 'google' or 'bing'.")
    try:
        page = _get_browser_page()
        if engine_normalized == "google":
            page.goto(
                f"https://www.google.com/search?q={quote_plus(query)}",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            items = page.query_selector_all("div.yuRUbf > a")[:5]
            results = []
            for item in items:
                title = item.inner_text().strip()
                href = item.get_attribute("href")
                if title:
                    results.append({"title": title, "url": href})
        else:
            page.goto(
                f"https://www.bing.com/search?q={quote_plus(query)}",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            items = page.query_selector_all("li.b_algo h2 a")[:5]
            results = [
                {"title": item.inner_text().strip(), "url": item.get_attribute("href")}
                for item in items
                if item.inner_text().strip()
            ]
        return _ok(results, f"Found {len(results)} search results for '{query}'.")
    except PlaywrightTimeoutError:
        return _err(f"Web search timed out for query: {query}")
    except Exception as exc:
        return _err(f"Web search failed: {exc}")


def browser_stress_50_sites(
    sites: str | None = None,
    site_count: int = 50,
    retries: int = 1,
    timeout_seconds: int = 12,
    wait_after_load_ms: int = 80,
    browser: str | None = None,
    require_http_ok: bool = True,
    open_each_in_new_tab: bool = True,
    dry_run: bool = False,
) -> dict:
    global _page_instance
    targets = _build_browser_stress_site_list(sites=sites, site_count=site_count)
    if not targets:
        return _err("No valid target sites found for browser stress run.")

    bounded_retries = _coerce_int(retries, default=1, minimum=0, maximum=5)
    bounded_timeout_seconds = _coerce_int(timeout_seconds, default=12, minimum=3, maximum=60)
    bounded_wait_ms = _coerce_int(wait_after_load_ms, default=80, minimum=0, maximum=5000)
    per_site_tabs = _coerce_bool(open_each_in_new_tab, default=True)

    if dry_run:
        simulated = [
            {
                "site": site,
                "status": "success",
                "attempts": [
                    {
                        "attempt": 1,
                        "status": "success",
                        "latency_seconds": 0.0,
                        "url": site,
                        "title": "dry-run",
                        "http_status": 200,
                        "error": "",
                    }
                ],
                "final_url": site,
                "title": "dry-run",
                "http_status": 200,
            }
            for site in targets
        ]
        payload = {
            "dry_run": True,
            "requested_sites": len(targets),
            "successful_sites": len(simulated),
            "failed_sites": 0,
            "retries_per_site": bounded_retries,
            "timeout_seconds": bounded_timeout_seconds,
            "open_each_in_new_tab": per_site_tabs,
            "tabs_opened": len(targets) if per_site_tabs else 1,
            "results": simulated,
        }
        return _ok(payload, f"Dry-run prepared deterministic stress run for {len(targets)} sites.")

    timeout_ms = bounded_timeout_seconds * 1000
    max_attempts = bounded_retries + 1

    try:
        shared_page = _get_browser_page(browser=browser)
        shared_page.bring_to_front()
        context = _browser_context
        if context is None or _context_is_closed(context):
            return _err("Browser stress prerequisites are not satisfied: browser context is unavailable.")
    except Exception as exc:
        return _err(f"Browser stress prerequisites are not satisfied: {exc}")

    site_results: list[dict] = []
    success_count = 0
    failed_sites: list[dict] = []
    overall_start = time.perf_counter()
    tabs_opened = 0

    for site in targets:
        attempts: list[dict] = []
        final_status = "error"
        final_url = ""
        final_title = ""
        final_http_status = None
        site_page = None

        for attempt_index in range(1, max_attempts + 1):
            attempt_start = time.perf_counter()
            attempt_record = {
                "attempt": attempt_index,
                "status": "error",
                "latency_seconds": 0.0,
                "url": "",
                "title": "",
                "http_status": None,
                "error": "",
            }
            try:
                if per_site_tabs:
                    if site_page is None or site_page.is_closed():
                        site_page = context.new_page()
                        tabs_opened += 1
                        _page_instance = site_page
                else:
                    if shared_page.is_closed():
                        shared_page = _get_browser_page(browser=browser)
                    site_page = shared_page

                site_page.bring_to_front()
                response = site_page.goto(site, wait_until="domcontentloaded", timeout=timeout_ms)
                if bounded_wait_ms > 0:
                    site_page.wait_for_timeout(bounded_wait_ms)

                http_status = response.status if response is not None else None
                if require_http_ok and (http_status is None or int(http_status) >= 400):
                    raise RuntimeError(f"HTTP status check failed with status={http_status}.")

                title = ""
                try:
                    title = site_page.title()
                except Exception:
                    title = ""

                attempt_record.update(
                    {
                        "status": "success",
                        "url": str(site_page.url),
                        "title": title[:180],
                        "http_status": http_status,
                    }
                )
            except PlaywrightTimeoutError:
                attempt_record["error"] = f"Timed out after {bounded_timeout_seconds}s."
            except Exception as exc:
                attempt_record["error"] = str(exc).replace("\n", " ").strip()[:280]
            finally:
                attempt_record["latency_seconds"] = round(time.perf_counter() - attempt_start, 3)
                attempts.append(attempt_record)

            if attempt_record["status"] == "success":
                final_status = "success"
                final_url = str(attempt_record.get("url", "")).strip()
                final_title = str(attempt_record.get("title", "")).strip()
                final_http_status = attempt_record.get("http_status")
                break

        if final_status == "success":
            success_count += 1
        else:
            failed_sites.append(
                {
                    "site": site,
                    "error": str(attempts[-1].get("error", "")).strip() if attempts else "Unknown failure.",
                }
            )

        site_results.append(
            {
                "site": site,
                "status": final_status,
                "attempts": attempts,
                "final_url": final_url,
                "title": final_title,
                "http_status": final_http_status,
            }
        )

    total_sites = len(targets)
    failure_count = total_sites - success_count
    payload = {
        "dry_run": False,
        "requested_sites": total_sites,
        "successful_sites": success_count,
        "failed_sites": failure_count,
        "retries_per_site": bounded_retries,
        "timeout_seconds": bounded_timeout_seconds,
        "open_each_in_new_tab": per_site_tabs,
        "tabs_opened": tabs_opened if per_site_tabs else 1,
        "elapsed_seconds": round(time.perf_counter() - overall_start, 3),
        "results": site_results,
        "failures": failed_sites,
    }

    if failure_count > 0:
        return {
            "status": "failure",
            "result": payload,
            "message": (
                f"Browser stress run failed for {failure_count}/{total_sites} sites after retries. "
                "Inspect result.failures for concrete diagnostics."
            ),
        }
    mode_text = "separate tabs" if per_site_tabs else "a single tab"
    return _ok(payload, f"Browser stress run succeeded across {total_sites} sites using {mode_text}.")


def save_text_to_desktop_file(
    content: str | None = None,
    filename: str | None = None,
    open_in_notepad: bool = False,
    from_clipboard: bool = False,
) -> dict:
    text = str(content or "")
    content_source = "provided_content"
    if not text.strip() and bool(from_clipboard):
        clipboard_text, clipboard_error = _get_clipboard_text()
        if clipboard_text is None:
            return _err(f"Clipboard read failed for Desktop file save: {clipboard_error}")
        text = str(clipboard_text)
        content_source = "clipboard"
    if not text.strip():
        return _err("No text content provided for Desktop file save.")

    try:
        desktop = _desktop_root()
    except Exception as exc:
        return _err(f"Desktop path is unavailable: {exc}")

    safe_name = _sanitize_desktop_filename(filename=filename, default_name=f"voco_export_{int(time.time())}.txt")
    target = desktop / safe_name
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        return _err(f"Failed to save Desktop file '{target}': {exc}")

    payload = {"path": str(target), "bytes": len(text), "opened_in_notepad": False, "source": content_source}
    if not open_in_notepad:
        return _ok(payload, f"Saved Desktop text file: {target}")

    notepad_target = _resolve_executable_target("notepad.exe")
    if not notepad_target:
        return _err(f"Desktop file was saved to '{target}', but Notepad is unavailable on this PC.")

    try:
        subprocess.Popen([notepad_target, str(target)], shell=False)
    except OSError as exc:
        return _err(f"Desktop file was saved to '{target}', but opening in Notepad failed: {exc}")

    payload["opened_in_notepad"] = True
    return _ok(payload, f"Saved Desktop text file and opened it in Notepad: {target}")


def _collect_youtube_comments(page, max_comments: int = 20, scroll_passes: int = 8) -> tuple[list[str], str | None]:
    bounded_comments = _coerce_int(max_comments, default=20, minimum=1, maximum=120)
    bounded_passes = _coerce_int(scroll_passes, default=8, minimum=1, maximum=25)

    try:
        page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 1.2));")
        page.wait_for_timeout(700)
    except Exception:
        pass

    try:
        page.wait_for_selector("ytd-comments", timeout=18000)
    except PlaywrightTimeoutError:
        body_text = ""
        try:
            body_text = page.inner_text("body")[:6000]
        except Exception:
            body_text = ""
        lower_body = body_text.lower()
        if "comments are turned off" in lower_body:
            return [], "Comments are turned off for this video."
        if "sign in to confirm your age" in lower_body or "sign in to continue" in lower_body:
            return [], "YouTube requires sign-in before comments can be viewed."
        return [], "YouTube comments section did not load in time."
    except Exception as exc:
        return [], f"Unable to load YouTube comments section: {exc}"

    comments: list[str] = []
    seen: set[str] = set()

    for _ in range(bounded_passes):
        try:
            nodes = page.query_selector_all(
                "ytd-comment-thread-renderer #content-text, ytd-comment-view-model #content-text"
            )
        except Exception:
            nodes = []

        for node in nodes:
            try:
                text = node.inner_text().strip()
            except Exception:
                continue
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if normalized in seen:
                continue
            seen.add(normalized)
            comments.append(normalized[:700])
            if len(comments) >= bounded_comments:
                return comments, None

        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9));")
            page.wait_for_timeout(700)
        except Exception:
            break

    if comments:
        return comments, None

    body_text = ""
    try:
        body_text = page.inner_text("body")[:6000]
    except Exception:
        body_text = ""
    lower_body = body_text.lower()
    if "comments are turned off" in lower_body:
        return [], "Comments are turned off for this video."
    if "sign in to confirm your age" in lower_body or "sign in to continue" in lower_body:
        return [], "YouTube requires sign-in before comments can be viewed."
    return [], "No comments were found on the loaded video page."


def youtube_comment_pipeline(
    query: str,
    comment_count: int = 20,
    output_filename: str | None = None,
    open_in_notepad: bool = False,
    pause_after_seconds: int = 2,
    browser: str | None = None,
    dry_run: bool = False,
) -> dict:
    search_query = str(query or "").strip()
    if not search_query:
        return _err("YouTube comment pipeline requires a non-empty query.")

    bounded_comment_count = _coerce_int(comment_count, default=20, minimum=1, maximum=120)
    pause_seconds = _coerce_int(pause_after_seconds, default=2, minimum=0, maximum=12)
    output_name = output_filename or f"youtube_comments_{int(time.time())}.txt"

    if dry_run:
        sample_comments = [f"[dry-run] Sample comment {idx + 1} for '{search_query}'." for idx in range(5)]
        dry_text = "\n".join(
            [
                f"Query: {search_query}",
                "Video: dry-run placeholder",
                "URL: https://www.youtube.com/watch?v=dry-run",
                "",
                "Comments:",
                *[f"{idx + 1}. {comment}" for idx, comment in enumerate(sample_comments)],
            ]
        )
        save_result = save_text_to_desktop_file(
            content=dry_text,
            filename=output_name,
            open_in_notepad=open_in_notepad,
        )
        if save_result["status"] != "success":
            return _err(f"YouTube pipeline dry-run could not save Desktop file: {save_result['message']}")
        saved_payload = save_result.get("result", {})
        return _ok(
            {
                "dry_run": True,
                "query": search_query,
                "comments_extracted": len(sample_comments),
                "comments": sample_comments,
                "output_path": saved_payload.get("path"),
            },
            "YouTube comment pipeline dry-run completed with Desktop export.",
        )

    search_result = search_youtube(search_query)
    if search_result["status"] != "success":
        return _err(f"YouTube pipeline failed at search step: {search_result['message']}")

    open_result = click_youtube_first_result()
    if open_result["status"] != "success":
        return _err(f"YouTube pipeline failed while opening the first result: {open_result['message']}")

    try:
        page = _get_browser_page(browser=browser)
        page.bring_to_front()
    except Exception as exc:
        return _err(f"YouTube pipeline could not acquire browser context: {exc}")

    current_url = str(page.url or "").lower()
    if "accounts.google.com" in current_url or "consent.youtube.com" in current_url:
        return _err(
            "YouTube redirected to sign-in/consent flow. Complete login/consent manually before extracting comments."
        )

    try:
        page.wait_for_selector("video", timeout=20000)
    except PlaywrightTimeoutError:
        return _err("YouTube video player did not load in time.")
    except Exception as exc:
        return _err(f"YouTube video player is unavailable: {exc}")

    if pause_seconds > 0:
        try:
            page.wait_for_timeout(pause_seconds * 1000)
        except Exception:
            pass

    paused = False
    try:
        paused = bool(
            page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (!v) return false;
                    if (!v.paused) v.pause();
                    return !!v.paused;
                }"""
            )
        )
    except Exception:
        paused = False

    if not paused:
        try:
            page.keyboard.press("k")
            page.wait_for_timeout(250)
            paused = bool(
                page.evaluate(
                    "() => { const v = document.querySelector('video'); return !!(v && v.paused); }"
                )
            )
        except Exception:
            paused = False

    if not paused:
        return _err("Unable to pause the opened YouTube video. Pipeline stopped to avoid fake success.")

    comments, comment_error = _collect_youtube_comments(page, max_comments=bounded_comment_count)
    if not comments:
        return _err(
            f"YouTube comment extraction failed: {comment_error or 'No comments were returned from the page.'}"
        )

    video_title = ""
    try:
        video_title = page.title()
    except Exception:
        video_title = ""
    parsed_url = urlparse(str(page.url))
    video_url = str(page.url)
    if parsed_url.netloc and "youtube.com" not in parsed_url.netloc and "youtu.be" not in parsed_url.netloc:
        return _err(
            "YouTube comment pipeline lost the target video page due to site redirect or availability change."
        )

    export_text = "\n".join(
        [
            f"Query: {search_query}",
            f"Video title: {video_title}",
            f"Video URL: {video_url}",
            f"Captured comments: {len(comments)}",
            "",
            "Comments:",
            *[f"{idx + 1}. {comment}" for idx, comment in enumerate(comments)],
        ]
    )
    save_result = save_text_to_desktop_file(
        content=export_text,
        filename=output_name,
        open_in_notepad=open_in_notepad,
    )
    if save_result["status"] != "success":
        return _err(f"YouTube comments were captured but Desktop export failed: {save_result['message']}")

    save_payload = save_result.get("result", {})
    return _ok(
        {
            "dry_run": False,
            "query": search_query,
            "video_title": video_title,
            "video_url": video_url,
            "comments_extracted": len(comments),
            "comments_preview": comments[:5],
            "output_path": save_payload.get("path"),
            "opened_in_notepad": bool(save_payload.get("opened_in_notepad")),
        },
        f"YouTube pipeline completed and saved {len(comments)} comments to Desktop.",
    )


def web_codegen_autofix(
    request: str,
    filename: str = "generated_script.py",
    max_fix_rounds: int = 2,
    run_timeout_seconds: int = 20,
    dry_run: bool = False,
) -> dict:
    task_request = str(request or "").strip()
    if not task_request:
        return _err("Code generation request cannot be empty.")

    raw_name = Path(str(filename or "").strip()).name
    safe_name = raw_name or "generated_script.py"
    if not safe_name.lower().endswith(".py"):
        safe_name = f"{safe_name}.py"
    safe_name = re.sub(r'[^a-zA-Z0-9._\-]+', "_", safe_name).strip("._") or "generated_script.py"

    try:
        target_path = _resolve_workspace_path(safe_name, allow_missing_parent=True)
    except Exception as exc:
        return _err(f"Invalid output filename '{filename}': {exc}")

    bounded_rounds = _coerce_int(max_fix_rounds, default=2, minimum=0, maximum=6)
    bounded_timeout = _coerce_int(run_timeout_seconds, default=20, minimum=5, maximum=120)

    attempts: list[dict] = []
    providers: list[str] = []

    if dry_run:
        dry_codes = [
            "def main() -> None:\n    print(undefined_symbol)\n\nif __name__ == '__main__':\n    main()\n",
            "def main() -> None:\n    print('dry run success')\n\nif __name__ == '__main__':\n    main()\n",
        ]
        for index, candidate_code in enumerate(dry_codes, start=1):
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(candidate_code)
            except Exception as exc:
                return _err(f"Dry-run failed while writing '{target_path}': {exc}")

            run_result = _run_python_file(target_path, timeout_seconds=bounded_timeout)
            run_success = bool(
                not run_result.get("timed_out")
                and int(run_result.get("returncode") or 0) == 0
            )
            attempts.append(
                {
                    "attempt": index,
                    "provider": "dry-run-seed" if index == 1 else "dry-run-fix",
                    "run_success": run_success,
                    "returncode": run_result.get("returncode"),
                    "timed_out": run_result.get("timed_out"),
                    "failure_summary": "" if run_success else _summarize_python_failure(run_result),
                    "stderr_excerpt": str(run_result.get("stderr", ""))[:600],
                    "stdout_excerpt": str(run_result.get("stdout", ""))[:600],
                }
            )
            if run_success:
                return _ok(
                    {
                        "dry_run": True,
                        "path": str(target_path),
                        "attempts": attempts,
                        "final_provider": attempts[-1]["provider"],
                    },
                    "Web-codegen autofix dry-run completed with deterministic rerun recovery.",
                )

        return {
            "status": "failure",
            "result": {"dry_run": True, "path": str(target_path), "attempts": attempts},
            "message": "Web-codegen autofix dry-run could not reach a successful rerun.",
        }

    previous_code: str | None = None
    last_error_text: str | None = None
    max_attempts = bounded_rounds + 1

    for attempt_index in range(1, max_attempts + 1):
        if attempt_index == 1:
            generation_prompt = (
                f"Write a complete Python script for this request:\n{task_request}\n"
                f"Output target filename: {target_path.name}\n"
                "Return only runnable Python code.\n"
                "The script must run non-interactively without requiring stdin input."
            )
        else:
            generation_prompt = (
                f"The previous Python code for '{target_path.name}' failed at runtime.\n"
                f"Original request:\n{task_request}\n\n"
                f"Current code:\n{previous_code or ''}\n\n"
                f"Runtime failure:\n{last_error_text or ''}\n\n"
                "Return a full corrected Python script only.\n"
                "The script must run non-interactively without requiring stdin input."
            )

        generated_ok, candidate_code, provider, generation_diagnostics = _request_codegen_candidate(
            prompt=generation_prompt,
            filename=target_path.name,
            previous_code=previous_code,
            error_text=last_error_text,
        )
        if not generated_ok:
            return {
                "status": "failure",
                "result": {
                    "path": str(target_path),
                    "attempts": attempts,
                    "generation_diagnostics": generation_diagnostics,
                },
                "message": (
                    "Web-codegen autofix could not obtain Python code from ChatGPT browser, configured web assistant, "
                    "or free AI path. "
                    + " | ".join(generation_diagnostics)
                )[:700],
            }

        providers.append(provider)
        previous_code = candidate_code

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(candidate_code)
        except Exception as exc:
            return _err(f"Web-codegen autofix failed writing '{target_path}': {exc}")

        run_result = _run_python_file(target_path, timeout_seconds=bounded_timeout)
        run_success = bool(
            not run_result.get("timed_out")
            and int(run_result.get("returncode") or 0) == 0
        )

        failure_summary = "" if run_success else _summarize_python_failure(run_result)
        attempts.append(
            {
                "attempt": attempt_index,
                "provider": provider,
                "run_success": run_success,
                "returncode": run_result.get("returncode"),
                "timed_out": run_result.get("timed_out"),
                "elapsed_seconds": run_result.get("elapsed_seconds"),
                "failure_summary": failure_summary,
                "stderr_excerpt": str(run_result.get("stderr", ""))[:900],
                "stdout_excerpt": str(run_result.get("stdout", ""))[:900],
            }
        )

        if run_success:
            return _ok(
                {
                    "dry_run": False,
                    "path": str(target_path),
                    "attempts": attempts,
                    "providers_used": providers,
                    "stdout": str(run_result.get("stdout", ""))[:1200],
                },
                f"Web-codegen autofix succeeded after {attempt_index} run(s).",
            )

        stderr_text = str(run_result.get("stderr", "")).strip()
        stdout_text = str(run_result.get("stdout", "")).strip()
        last_error_text = "\n".join(
            [part for part in [stderr_text, stdout_text] if part]
        )[:3000] or failure_summary

    final_attempt = attempts[-1] if attempts else {}
    return {
        "status": "failure",
        "result": {
            "dry_run": False,
            "path": str(target_path),
            "attempts": attempts,
            "providers_used": providers,
            "last_failure": final_attempt,
        },
        "message": (
            f"Web-codegen autofix exhausted {max_attempts} attempt(s). "
            f"Last failure: {final_attempt.get('failure_summary') or 'unknown runtime failure'}"
        )[:700],
    }


def type_in_browser(
    text: str,
    selector: str | None = None,
    multiline: bool | None = None,
    submit: bool = False,
    newline_mode: str | None = None,
) -> dict:
    typed = str(text)
    if not typed:
        return _err("No text provided for browser typing.")
    try:
        page = _get_browser_page()
        page.bring_to_front()
        if selector:
            target = page.wait_for_selector(selector, timeout=5000)
            target.click()
            normalized_multiline = bool(multiline) or ("\n" in typed)
            resolved_newline_mode = _normalize_newline_mode(
                newline_mode=newline_mode,
                multiline=multiline,
                typed=typed,
            )
            if normalized_multiline:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                _type_text_with_newline_policy(page, typed, resolved_newline_mode)
            else:
                target.fill(typed)
            if bool(submit):
                page.keyboard.press("Enter")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    pass
            state = _snapshot_browser_state(page)
            if normalized_multiline and not bool(submit):
                return _ok(
                    state,
                    f"Typed multiline text in browser selector '{selector}' using {resolved_newline_mode}.",
                )
            if bool(submit):
                return _ok(state, f"Typed text in browser selector '{selector}' and submitted.")
            return _ok(state, f"Typed text in browser selector '{selector}'.")
        return browser_type(
            text=typed,
            element_name=None,
            clear=True,
            multiline=multiline,
            submit=submit,
            newline_mode=newline_mode,
        )
    except PlaywrightTimeoutError:
        return _err(f"Browser selector not found: {selector}")
    except Exception as exc:
        return _err(f"Browser typing failed: {exc}")


def press_key_in_browser(key: str) -> dict:
    return browser_press_key(key=key)


def click_in_browser(text: str | None = None, selector: str | None = None) -> dict:
    try:
        page = _get_browser_page()
        page.bring_to_front()
        if selector:
            locator = page.locator(selector).first
            locator.wait_for(timeout=8000)
            locator.click()
            state = _snapshot_browser_state(page)
            return _ok(state, f"Clicked browser element by selector: {selector}")

        if text:
            return browser_click(element_name=text)

        return _err("Provide either selector or text to click in browser.")
    except PlaywrightTimeoutError:
        return _err("Timed out while clicking browser element.")
    except Exception as exc:
        return _err(f"Browser click failed: {exc}")


def _focus_best_text_input(page) -> bool:
    selector_candidates = [
        "textarea",
        "div[role='textbox']",
        "[contenteditable='true']",
        "input[type='text']",
        "input[type='search']",
        "input:not([type])",
    ]
    for selector in selector_candidates:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1200)
            locator.click()
            return True
        except Exception:
            continue
    return False


def get_page_content() -> dict:
    try:
        page = _get_browser_page()
        content = page.inner_text("body")[:3000]
        state = _snapshot_browser_state(page)
        state["content"] = content
        return _ok(state, f"Captured page text from {page.url}")
    except Exception as exc:
        return _err(f"Failed to capture page content: {exc}")


def _normalize_extension(extension: str) -> str | None:
    normalized = str(extension or "").strip().lower()
    if not normalized:
        return None
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", normalized):
        return None
    return normalized


def _resolve_app_paths_registry(executable_name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None

    leaf = Path(str(executable_name).strip().strip('"')).name
    if not leaf:
        return None
    if "." not in leaf:
        leaf = f"{leaf}.exe"

    registry_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, base_key in registry_roots:
        key_path = f"{base_key}\\{leaf}"
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        resolved = str(value).strip().strip('"')
        if not resolved:
            continue
        expanded = os.path.expandvars(resolved)
        if Path(expanded).exists():
            return expanded
        return expanded
    return None


def _resolve_executable_target(executable: str) -> str | None:
    raw = str(executable or "").strip().strip('"')
    if not raw:
        return None
    expanded = os.path.expandvars(raw)
    if expanded.lower().startswith("ms-"):
        return expanded

    path_candidate = Path(expanded)
    if path_candidate.is_absolute() or "\\" in expanded or "/" in expanded:
        if path_candidate.exists():
            return str(path_candidate)
        if path_candidate.suffix:
            return None
        maybe_exe = Path(f"{expanded}.exe")
        if maybe_exe.exists():
            return str(maybe_exe)
        return None

    resolved = shutil.which(expanded)
    if resolved:
        return resolved

    if "." not in Path(expanded).name:
        resolved = shutil.which(f"{expanded}.exe")
        if resolved:
            return resolved

    return _resolve_app_paths_registry(expanded)


def _resolve_app_target(app_name: str) -> tuple[str, str, str]:
    requested = str(app_name).strip()
    indexed_match = _lookup_indexed_app(requested)
    if indexed_match is not None:
        matched_name, executable = indexed_match
        return matched_name, executable, "index"

    normalized = requested.lower()
    if normalized in _COMMON_APP_ALIASES:
        return requested, _COMMON_APP_ALIASES[normalized], "builtin"

    return requested, requested, "direct"


def _app_unavailable_hint(app_name: str) -> str:
    normalized = app_name.strip().lower()
    if normalized == "spotify":
        return "Install Spotify Desktop (Microsoft Store) or use https://open.spotify.com."
    if normalized in {"powerpoint", "power point", "ppt", "pptx"}:
        return "Install Microsoft PowerPoint (Office) or use PowerPoint Online."
    return "Install the app or run /index-app to refresh the local app index."


def _extract_executable_from_open_command(command_line: str) -> str | None:
    command = str(command_line or "").strip()
    if not command:
        return None
    if command.startswith('"'):
        closing_quote = command.find('"', 1)
        if closing_quote > 1:
            return command[1:closing_quote].strip()
    token = command.split(maxsplit=1)[0]
    cleaned = token.strip().strip('"')
    return cleaned or None


def _extension_unavailable_hint(extension: str) -> str:
    if extension == ".pdf":
        return "Install a PDF app and set it in Settings > Apps > Default apps."
    if extension in {".ppt", ".pptx"}:
        return "Install Microsoft PowerPoint and set it as the default app for PPT/PPTX."
    return "Set a default app for this extension in Settings > Apps > Default apps."


def _lookup_file_handler(extension: str) -> dict:
    normalized = _normalize_extension(extension)
    if normalized is None:
        return {
            "extension": str(extension),
            "available": False,
            "prog_id": None,
            "command": None,
            "executable": None,
            "resolved_executable": None,
        }

    try:
        import winreg
    except ImportError:
        return {
            "extension": normalized,
            "available": False,
            "prog_id": None,
            "command": None,
            "executable": None,
            "resolved_executable": None,
        }

    prog_id: str | None = None
    user_choice_key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{normalized}\UserChoice"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, user_choice_key) as key:
            raw_prog_id, _ = winreg.QueryValueEx(key, "ProgId")
            candidate = str(raw_prog_id).strip()
            if candidate:
                prog_id = candidate
    except OSError:
        pass

    if not prog_id:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, normalized) as key:
                raw_prog_id, _ = winreg.QueryValueEx(key, None)
                candidate = str(raw_prog_id).strip()
                if candidate:
                    prog_id = candidate
        except OSError:
            prog_id = None

    command: str | None = None
    if prog_id:
        command_key = rf"{prog_id}\shell\open\command"
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, command_key) as key:
                raw_command, _ = winreg.QueryValueEx(key, None)
                candidate_command = str(raw_command).strip()
                if candidate_command:
                    command = candidate_command
        except OSError:
            command = None

    executable = _extract_executable_from_open_command(command) if command else None
    resolved_executable = _resolve_executable_target(executable) if executable else None

    return {
        "extension": normalized,
        "available": bool(command and resolved_executable),
        "prog_id": prog_id,
        "command": command,
        "executable": executable,
        "resolved_executable": resolved_executable,
    }


def _ensure_index_parent() -> None:
    _FILE_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    _APP_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)


def _get_drive_roots() -> list[Path]:
    roots: list[Path] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        if root.exists():
            roots.append(root)
    return roots


_TEXT_CONTEXT_MAX_BYTES = 12 * 1024
_TEXT_CONTEXT_MAX_CHARS = 1200
_TEXT_CONTEXT_SNIPPET_CHARS = 300
_TEXT_CONTEXT_SUMMARY_CHARS = 240
_TEXT_CONTEXT_MAX_KEYWORDS = 12
_TEXT_CONTEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".log",
        ".ini",
        ".cfg",
        ".conf",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".jsonl",
        ".xml",
        ".csv",
        ".tsv",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".java",
        ".kt",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
    }
)
_TEXT_CONTEXT_FILENAMES = frozenset(
    {
        "dockerfile",
        "makefile",
        "cmakelists.txt",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".env",
        "requirements.txt",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
    }
)
_TEXT_CONTEXT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
        "not",
        "can",
        "all",
        "any",
        "but",
        "into",
        "out",
        "our",
        "their",
        "they",
        "them",
        "if",
        "else",
        "true",
        "false",
        "null",
        "none",
        "def",
        "class",
        "return",
        "import",
        "const",
        "let",
        "var",
        "function",
    }
)


def _supports_context_extraction(path: Path) -> bool:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in _TEXT_CONTEXT_EXTENSIONS:
        return True
    if name in _TEXT_CONTEXT_FILENAMES:
        return True
    if name.startswith(".env"):
        return True
    if suffix == "" and name.startswith("readme"):
        return True
    return False


def _read_bounded_file_chunk(path: Path, max_bytes: int = _TEXT_CONTEXT_MAX_BYTES) -> bytes | None:
    try:
        with path.open("rb") as file_obj:
            return file_obj.read(max(512, int(max_bytes)))
    except (PermissionError, OSError):
        return None


def _is_probably_binary_chunk(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    control_count = sum(1 for byte in data if byte < 32 and byte not in (9, 10, 13))
    return (control_count / len(data)) > 0.15


def _decode_text_chunk(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _normalize_context_text(text: str, limit: int = _TEXT_CONTEXT_MAX_CHARS) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > limit:
        compact = compact[:limit].rstrip()
    return compact


def _derive_context_keywords(text: str, limit: int = _TEXT_CONTEXT_MAX_KEYWORDS) -> list[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    if not tokens:
        return []

    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, token in enumerate(tokens):
        if token in _TEXT_CONTEXT_STOPWORDS or token.isnumeric():
            continue
        counts[token] += 1
        if token not in first_seen:
            first_seen[token] = idx

    ranked = sorted(counts.items(), key=lambda item: (-item[1], first_seen[item[0]], item[0]))
    return [token for token, _ in ranked[: max(1, int(limit))]]


def _derive_context_summary(text: str, keywords: list[str]) -> str | None:
    if not text:
        return None

    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", text) if segment.strip()]
    summary = sentences[0] if sentences else text
    if len(summary) < 72 and len(sentences) > 1:
        for sentence in sentences[1:]:
            summary = f"{summary} {sentence}".strip()
            if len(summary) >= 72:
                break

    summary = summary[:_TEXT_CONTEXT_SUMMARY_CHARS].rstrip(" ,;:-")
    if not summary:
        return None
    if not keywords:
        return summary

    topics = ", ".join(keywords[:4])
    suffix = f" | topics: {topics}"
    max_summary_chars = _TEXT_CONTEXT_SUMMARY_CHARS - len(suffix)
    if max_summary_chars > 24 and len(summary) > max_summary_chars:
        summary = summary[:max_summary_chars].rstrip(" ,;:-")
    if len(summary) + len(suffix) <= _TEXT_CONTEXT_SUMMARY_CHARS:
        summary = f"{summary}{suffix}"
    return summary


def _compute_context_hash(data: bytes, size_bytes: int) -> str:
    digest = hashlib.sha256()
    digest.update(str(size_bytes).encode("utf-8"))
    digest.update(b":")
    digest.update(data)
    return digest.hexdigest()


def _extract_file_context(path: Path, size_bytes: int) -> tuple[str | None, str | None, str | None, str | None]:
    if not _supports_context_extraction(path):
        return (None, None, None, None)

    chunk = _read_bounded_file_chunk(path)
    if not chunk or _is_probably_binary_chunk(chunk):
        return (None, None, None, None)

    text = _decode_text_chunk(chunk)
    normalized = _normalize_context_text(text)
    if not normalized:
        return (None, None, None, None)

    snippet = normalized[:_TEXT_CONTEXT_SNIPPET_CHARS].rstrip(" ,;:-")
    keywords = _derive_context_keywords(normalized)
    summary = _derive_context_summary(normalized, keywords)
    content_hash = _compute_context_hash(chunk, size_bytes)
    keywords_text = ", ".join(keywords) if keywords else None
    return (snippet or None, keywords_text, summary, content_hash)


def _ensure_files_index_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT,
            size_bytes INTEGER,
            modified_ts REAL,
            content_snippet TEXT,
            content_keywords TEXT,
            content_summary TEXT,
            content_hash TEXT
        )
        """
    )

    context_columns = (
        "content_snippet",
        "content_keywords",
        "content_summary",
        "content_hash",
    )
    existing_columns = {
        str(row[1]).strip().lower() for row in conn.execute("PRAGMA table_info(files)").fetchall()
    }
    for column in context_columns:
        if column in existing_columns:
            continue
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def index_files(scope: str = "quick", max_files: int = 200000) -> dict:
    _ensure_index_parent()
    scope_normalized = str(scope).strip().lower()
    full_scan = scope_normalized in {"full", "all", "pc", "this-pc"}
    cap = max(1000, int(max_files))
    roots = _get_drive_roots() if full_scan else _default_search_roots()
    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "Windows",
        "System32",
        "SysWOW64",
        "Program Files",
        "Program Files (x86)",
        "ProgramData",
        "AppData",
        "$Recycle.Bin",
        "System Volume Information",
    }

    indexed = 0
    with _INDEX_LOCK:
        conn = sqlite3.connect(_FILE_INDEX_DB)
        try:
            _ensure_files_index_schema(conn)
            conn.execute("DELETE FROM files")
            batch: list[
                tuple[
                    str,
                    str,
                    str,
                    int,
                    float,
                    str | None,
                    str | None,
                    str | None,
                    str | None,
                ]
            ] = []
            for root in roots:
                for current, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if d not in skip_dirs]
                    for filename in files:
                        full_path = Path(current) / filename
                        try:
                            stat = full_path.stat()
                        except (PermissionError, OSError):
                            continue
                        content_snippet, content_keywords, content_summary, content_hash = _extract_file_context(
                            full_path,
                            int(stat.st_size),
                        )
                        batch.append(
                            (
                                str(full_path),
                                filename,
                                full_path.suffix.lower(),
                                int(stat.st_size),
                                float(stat.st_mtime),
                                content_snippet,
                                content_keywords,
                                content_summary,
                                content_hash,
                            )
                        )
                        indexed += 1
                        if len(batch) >= 1000:
                            conn.executemany(
                                """
                                INSERT INTO files (
                                    path,
                                    filename,
                                    extension,
                                    size_bytes,
                                    modified_ts,
                                    content_snippet,
                                    content_keywords,
                                    content_summary,
                                    content_hash
                                ) VALUES (?,?,?,?,?,?,?,?,?)
                                """,
                                batch,
                            )
                            conn.commit()
                            batch.clear()
                        if indexed >= cap:
                            break
                    if indexed >= cap:
                        break
                if indexed >= cap:
                    break
            if batch:
                conn.executemany(
                    """
                    INSERT INTO files (
                        path,
                        filename,
                        extension,
                        size_bytes,
                        modified_ts,
                        content_snippet,
                        content_keywords,
                        content_summary,
                        content_hash
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    batch,
                )
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_filename_lower ON files(lower(filename))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_path_lower ON files(lower(path))")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_extension_modified_ts ON files(extension, modified_ts)"
            )
            conn.commit()
        finally:
            conn.close()

    scope_label = "full-PC" if full_scan else "quick"
    return _ok(
        {"scope": scope_label, "files_indexed": indexed, "db_path": str(_FILE_INDEX_DB)},
        f"Indexed {indexed} files ({scope_label} scan).",
    )


def index_apps() -> dict:
    _ensure_index_parent()
    discovered: dict[str, tuple[str, str]] = {}

    # Registry App Paths provides launchable executables for installed apps.
    try:
        import winreg
    except ImportError:
        winreg = None

    if winreg is not None:
        registry_roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        for hive, subkey in registry_roots:
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            index = 0
            while True:
                try:
                    app_key_name = winreg.EnumKey(key, index)
                    index += 1
                except OSError:
                    break
                try:
                    app_key = winreg.OpenKey(key, app_key_name)
                    executable, _ = winreg.QueryValueEx(app_key, None)
                except OSError:
                    continue
                if not executable:
                    continue
                name = Path(app_key_name).stem.lower()
                discovered[name] = (str(executable), "registry")

    # Keep common aliases even without registry coverage.
    for name, executable in _COMMON_APP_ALIASES.items():
        discovered.setdefault(name, (executable, "builtin"))

    with _INDEX_LOCK:
        conn = sqlite3.connect(_APP_INDEX_DB)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS apps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    executable_path TEXT NOT NULL,
                    source TEXT
                )
                """
            )
            conn.execute("DELETE FROM apps")
            rows = [(name, value[0], value[1]) for name, value in discovered.items()]
            if rows:
                conn.executemany(
                    "INSERT INTO apps (name, executable_path, source) VALUES (?,?,?)",
                    rows,
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_name_lower ON apps(lower(name))")
            conn.commit()
        finally:
            conn.close()

    return _ok(
        {"apps_indexed": len(discovered), "db_path": str(_APP_INDEX_DB)},
        f"Indexed {len(discovered)} launchable applications.",
    )


def _lookup_indexed_app(app_name: str) -> tuple[str, str] | None:
    if not _APP_INDEX_DB.exists():
        return None
    query = app_name.strip().lower()
    if not query:
        return None
    conn = sqlite3.connect(_APP_INDEX_DB)
    try:
        cursor = conn.execute(
            """
            SELECT name, executable_path
            FROM apps
            WHERE lower(name) LIKE ?
            ORDER BY length(name) ASC
            LIMIT 1
            """,
            (f"%{query}%",),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])
    finally:
        conn.close()


def check_app_availability(app_name: str) -> dict:
    requested = str(app_name).strip()
    if not requested:
        return _err("Application name cannot be empty.")

    matched_name, executable, source = _resolve_app_target(requested)
    launch_target = _resolve_executable_target(executable)
    available = bool(launch_target)

    payload = {
        "requested_app": requested,
        "matched_app": matched_name,
        "source": source,
        "configured_executable": executable,
        "launch_target": launch_target,
        "available": available,
    }
    if available:
        return _ok(payload, f"'{matched_name}' is available and launchable.")

    hint = _app_unavailable_hint(requested)
    payload["fallback"] = hint
    return _ok(payload, f"'{requested}' is not launchable on this PC. {hint}")


def _normalize_window_search_term(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    normalized = normalized.replace("ms-settings:", "settings")
    normalized = normalized.replace("msedge", "edge")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _collect_app_window_terms(
    requested: str, matched_name: str, executable: str, launch_target: str
) -> list[str]:
    alias_terms: dict[str, tuple[str, ...]] = {
        "notepad": ("notepad",),
        "calc": ("calculator",),
        "calculator": ("calculator",),
        "mspaint": ("paint",),
        "paint": ("paint",),
        "explorer": ("file explorer", "explorer"),
        "taskmgr": ("task manager",),
        "task manager": ("task manager",),
        "powerpnt": ("powerpoint",),
        "ppt": ("powerpoint",),
        "pptx": ("powerpoint",),
        "ms settings": ("settings",),
        "settings": ("settings",),
        "edge": ("edge", "microsoft edge"),
        "chrome": ("chrome", "google chrome"),
        "firefox": ("firefox",),
        "spotify": ("spotify",),
        "powershell": ("powershell",),
        "cmd": ("command prompt", "cmd"),
    }
    ignored_terms = {"app", "application", "program", "launcher", "windows", "microsoft"}
    seen_terms: set[str] = set()
    terms: list[str] = []

    def add_term(term: str) -> None:
        normalized = _normalize_window_search_term(term)
        if not normalized or normalized in ignored_terms or normalized in seen_terms:
            return
        if len(normalized) < 3:
            return
        seen_terms.add(normalized)
        terms.append(normalized)

    raw_candidates = [requested, matched_name, executable, launch_target]
    for candidate in raw_candidates:
        add_term(candidate)
        candidate_str = str(candidate or "").strip()
        if candidate_str:
            add_term(Path(candidate_str).stem)
        normalized_candidate = _normalize_window_search_term(candidate)
        for token in normalized_candidate.split():
            if len(token) >= 4:
                add_term(token)
        for key in (normalized_candidate, *normalized_candidate.split()):
            for alias in alias_terms.get(key, ()):
                add_term(alias)

    return terms


def _find_matching_window(search_terms: list[str]):
    if not PYGETWINDOW_AVAILABLE or not search_terms:
        return None
    try:
        windows = gw.getAllWindows()
    except Exception:
        return None
    for window in windows:
        title = str(getattr(window, "title", "") or "").strip()
        if not title:
            continue
        lowered_title = title.lower()
        if any(term in lowered_title for term in search_terms):
            return window
    return None


def _focus_window_handle(window) -> tuple[bool, str]:
    if window is None:
        return False, "No matching window handle was found."
    try:
        if getattr(window, "isMinimized", False):
            window.restore()
        window.activate()
        time.sleep(0.2)
        return True, str(getattr(window, "title", ""))
    except Exception as exc:
        return False, str(exc)


def _wait_and_focus_window(search_terms: list[str], timeout_seconds: float = 6.0) -> tuple[object | None, str]:
    if not PYGETWINDOW_AVAILABLE:
        return None, "pygetwindow is not installed."
    deadline = time.time() + max(0.5, float(timeout_seconds))
    last_error = ""
    while time.time() < deadline:
        window = _find_matching_window(search_terms)
        if window is not None:
            focused, note = _focus_window_handle(window)
            if focused:
                return window, note
            last_error = note
        time.sleep(0.2)
    if last_error:
        return None, last_error
    return None, "Matching window did not appear in time."


def open_app(app_name: str, force_new: bool = False) -> dict:
    requested = str(app_name).strip()
    if not requested:
        return _err("Application name cannot be empty.")

    availability = check_app_availability(requested)
    if availability["status"] != "success":
        return availability
    payload = availability.get("result", {})
    if not isinstance(payload, dict):
        return _err(f"Cannot evaluate app availability for '{requested}'.")

    launch_target = str(payload.get("launch_target") or "").strip()
    matched_name = str(payload.get("matched_app") or requested)
    executable = str(payload.get("configured_executable") or requested)
    if not launch_target:
        fallback = str(payload.get("fallback") or _app_unavailable_hint(requested))
        return _err(f"Application '{requested}' is not launchable on this PC. {fallback}")

    search_terms = _collect_app_window_terms(requested, matched_name, executable, launch_target)
    if not bool(force_new) and search_terms:
        existing_window = _find_matching_window(search_terms)
        if existing_window is not None:
            focused, focused_title = _focus_window_handle(existing_window)
            if focused:
                return _ok(
                    {
                        "app": matched_name,
                        "executable": executable,
                        "launch_target": launch_target,
                        "source": payload.get("source"),
                        "window": focused_title or matched_name,
                        "action": "focused_existing_window",
                        "force_new": False,
                    },
                    f"Focused existing application: {matched_name}",
                )

    try:
        if launch_target.lower().startswith("ms-"):
            subprocess.Popen(["cmd", "/c", "start", "", launch_target], shell=False)
        else:
            subprocess.Popen([launch_target], shell=False)
    except OSError as exc:
        return _err(f"Failed to open app '{requested}': {exc}")

    time.sleep(0.8)
    return _ok(
        {
            "app": matched_name,
            "executable": executable,
            "launch_target": launch_target,
            "source": payload.get("source"),
            "action": "launched_new_instance",
            "force_new": bool(force_new),
        },
        f"Opened application: {matched_name}",
    )


def check_file_handler(extension: str) -> dict:
    normalized = _normalize_extension(extension)
    if normalized is None:
        return _err("Invalid extension. Use values like .pdf, pdf, .ppt, or .pptx.")

    handler = _lookup_file_handler(normalized)
    if handler["available"]:
        executable = handler.get("resolved_executable")
        return _ok(handler, f"Default handler for '{normalized}' is available: {executable}")

    hint = _extension_unavailable_hint(normalized)
    handler["fallback"] = hint
    return _ok(handler, f"No launchable default handler found for '{normalized}'. {hint}")


def _document_extension_set(extension: str) -> tuple[str, ...] | None:
    normalized = _normalize_extension(extension)
    if normalized == ".pdf":
        return (".pdf",)
    if normalized in {".ppt", ".pptx"}:
        return (".ppt", ".pptx")
    return None


def _search_indexed_documents(
    extensions: tuple[str, ...],
    query: str | None,
    limit: int,
) -> list[dict]:
    if not _FILE_INDEX_DB.exists():
        return []

    cap = max(1, int(limit))
    normalized_query = str(query or "").strip().lower()
    placeholders = ", ".join("?" for _ in extensions)
    sql = (
        "SELECT path, filename, extension, modified_ts FROM files "
        f"WHERE extension IN ({placeholders})"
    )
    params: list[object] = list(extensions)
    if normalized_query:
        sql += " AND (lower(filename) LIKE ? OR lower(path) LIKE ?)"
        like = f"%{normalized_query}%"
        params.extend([like, like])
    sql += " ORDER BY modified_ts DESC LIMIT ?"
    params.append(cap * 4)

    conn = sqlite3.connect(_FILE_INDEX_DB)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    matches: list[dict] = []
    extension_set = set(extensions)
    for row in rows:
        path_str, filename, row_extension, modified_ts = row
        path_obj = Path(str(path_str))
        if not path_obj.is_file():
            continue
        suffix = str(row_extension or path_obj.suffix).lower()
        if suffix not in extension_set:
            continue
        matches.append(
            {
                "path": str(path_obj),
                "filename": str(filename or path_obj.name),
                "extension": suffix,
                "modified_ts": float(modified_ts or 0.0),
                "source": "index",
            }
        )
        if len(matches) >= cap:
            break
    return matches


def _search_documents_live(extensions: tuple[str, ...], query: str | None, limit: int) -> list[dict]:
    cap = max(1, int(limit))
    normalized_query = str(query or "").strip().lower()
    extension_set = set(extensions)

    roots: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE", ""))
    if user_profile.exists():
        root_candidates = [
            user_profile / "Desktop",
            user_profile / "Documents",
            user_profile / "Downloads",
            user_profile / "OneDrive" / "Desktop",
            user_profile / "OneDrive" / "Documents",
        ]
        roots.extend(candidate for candidate in root_candidates if candidate.exists())
    roots.extend(_default_search_roots())

    deduped_roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen_roots:
            continue
        seen_roots.add(key)
        deduped_roots.append(root)

    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "Windows",
        "Program Files",
        "Program Files (x86)",
        "ProgramData",
        "AppData",
        "$Recycle.Bin",
        "System Volume Information",
    }
    deadline = time.time() + 15
    candidates: list[dict] = []

    for root in deduped_roots:
        for current, dirs, files in os.walk(root):
            if time.time() > deadline:
                break
            dirs[:] = [d for d in dirs if d not in skip_dirs]

            for filename in files:
                suffix = Path(filename).suffix.lower()
                if suffix not in extension_set:
                    continue
                full_path = Path(current) / filename
                haystack = f"{filename.lower()} {str(full_path).lower()}"
                if normalized_query and normalized_query not in haystack:
                    continue
                try:
                    modified_ts = float(full_path.stat().st_mtime)
                except (PermissionError, OSError):
                    modified_ts = 0.0
                candidates.append(
                    {
                        "path": str(full_path),
                        "filename": filename,
                        "extension": suffix,
                        "modified_ts": modified_ts,
                        "source": "live-scan",
                    }
                )
                if len(candidates) >= cap * 4:
                    break
            if len(candidates) >= cap * 4 or time.time() > deadline:
                break
        if len(candidates) >= cap * 4 or time.time() > deadline:
            break

    candidates.sort(key=lambda row: float(row.get("modified_ts") or 0.0), reverse=True)
    return candidates[:cap]


def open_existing_document(extension: str, query: str | None = None, limit: int = 20) -> dict:
    extensions = _document_extension_set(extension)
    if extensions is None:
        return _err("Unsupported document extension. Use .pdf, .ppt, or .pptx.")

    cap = _coerce_int(limit, default=20, minimum=1, maximum=100)
    normalized_query = str(query or "").strip()
    handler_check = check_file_handler(".pdf" if ".pdf" in extensions else ".pptx")
    if handler_check["status"] != "success":
        return handler_check
    handler_payload = handler_check.get("result", {})
    if isinstance(handler_payload, dict) and not bool(handler_payload.get("available")):
        return _err(
            "Cannot open existing document because no launchable default handler is configured. "
            f"{handler_check['message']}"
        )

    matches = _search_indexed_documents(extensions=extensions, query=normalized_query, limit=cap)
    source = "index"
    if not matches:
        matches = _search_documents_live(extensions=extensions, query=normalized_query, limit=cap)
        source = "live-scan"

    if not matches:
        query_suffix = f" matching '{normalized_query}'" if normalized_query else ""
        return _err(f"No existing {', '.join(extensions)} files were found on this PC{query_suffix}.")

    selected = matches[0]
    open_result = open_file_with_default_app(selected["path"])
    if open_result["status"] != "success":
        return _err(
            f"Found an existing file but opening failed: {selected['path']}. "
            f"{open_result['message']}"
        )

    opened_payload = open_result.get("result", {})
    return _ok(
        {
            "query": normalized_query,
            "selected_path": selected["path"],
            "selected_extension": selected["extension"],
            "source": source,
            "candidate_count": len(matches),
            "opened": opened_payload,
        },
        f"Opened existing document: {selected['path']}",
    )


def open_file_with_default_app(path: str) -> dict:
    raw_path = str(path).strip().strip("\"'")
    if not raw_path:
        return _err("File path cannot be empty.")

    expanded = Path(os.path.expandvars(raw_path)).expanduser()
    file_path = expanded if expanded.is_absolute() else (Path.cwd() / expanded)

    try:
        resolved = file_path.resolve(strict=True)
    except FileNotFoundError:
        return _err(f"File not found: {file_path}")
    except OSError as exc:
        return _err(f"Invalid file path '{raw_path}': {exc}")

    if not resolved.is_file():
        return _err(f"Path is not a file: {resolved}")

    extension = resolved.suffix.lower()
    if extension in {".pdf", ".ppt", ".pptx"}:
        handler_check = check_file_handler(extension)
        if handler_check["status"] != "success":
            return handler_check
        handler_payload = handler_check.get("result", {})
        if isinstance(handler_payload, dict) and not bool(handler_payload.get("available")):
            return _err(
                f"Cannot open '{resolved.name}' because no launchable default handler is configured for "
                f"'{extension}'. {handler_check['message']}"
            )

    try:
        os.startfile(str(resolved))
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1155:
            hint = _extension_unavailable_hint(extension or "file")
            return _err(
                f"No app is associated with '{extension or 'this file type'}'. "
                f"{hint}"
            )
        return _err(f"Failed to open file '{resolved}': {exc}")

    return _ok(
        {"path": str(resolved), "extension": extension},
        f"Opened file with default app: {resolved}",
    )


def open_extension_handler(extension: str) -> dict:
    normalized = _normalize_extension(extension)
    if normalized is None:
        return _err("Invalid extension. Use values like .pdf, pdf, .ppt, or .pptx.")

    handler_check = check_file_handler(normalized)
    if handler_check["status"] != "success":
        return handler_check
    payload = handler_check.get("result", {})
    if not isinstance(payload, dict):
        return _err(f"Could not inspect default handler for '{normalized}'.")
    if not payload.get("available"):
        return _err(handler_check["message"])

    launch_target = str(payload.get("resolved_executable") or "").strip()
    if not launch_target:
        return _err(f"Could not resolve executable for '{normalized}' handler.")

    try:
        subprocess.Popen([launch_target], shell=False)
    except OSError as exc:
        return _err(f"Failed to open '{normalized}' default handler: {exc}")

    return _ok(
        {
            "extension": normalized,
            "handler_prog_id": payload.get("prog_id"),
            "launch_target": launch_target,
        },
        f"Opened default handler for '{normalized}'.",
    )


def _focus_spotify_window(timeout_seconds: float = 8.0) -> tuple[bool, str]:
    if not PYGETWINDOW_AVAILABLE:
        return False, "pygetwindow is not installed."

    deadline = time.time() + max(2.0, float(timeout_seconds))
    last_error = ""
    while time.time() < deadline:
        try:
            windows = gw.getWindowsWithTitle("Spotify")
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue

        if windows:
            window = windows[0]
            try:
                if getattr(window, "isMinimized", False):
                    window.restore()
                window.activate()
                return True, str(getattr(window, "title", "Spotify"))
            except Exception as exc:
                last_error = str(exc)
        time.sleep(0.25)

    if last_error:
        return False, f"Spotify window was found but could not be focused: {last_error}"
    return False, "Spotify window did not appear in time."


def spotify_play(query: str | None = None) -> dict:
    normalized_query = str(query or "").strip()

    availability = check_app_availability("spotify")
    if availability["status"] != "success":
        return availability
    payload = availability.get("result", {})
    if not isinstance(payload, dict) or not payload.get("available"):
        fallback = ""
        if isinstance(payload, dict):
            fallback = str(payload.get("fallback") or "")
        fallback_text = fallback or _app_unavailable_hint("spotify")
        return _err(f"Spotify is not available on this PC. {fallback_text}")

    open_result = open_app("spotify")
    if open_result["status"] != "success":
        return open_result

    if not normalized_query:
        return _ok(
            {"opened": True, "play_attempted": False, "query": None},
            "Opened Spotify. No track/query specified to play.",
        )

    missing = []
    if not PYGETWINDOW_AVAILABLE:
        missing.append("pygetwindow")
    if not PYAUTOGUI_AVAILABLE:
        missing.append("pyautogui")
    if missing:
        deps = ", ".join(missing)
        return _err(
            f"Opened Spotify, but cannot automate search/play because {deps} is not installed. "
            "Play the track manually inside Spotify."
        )

    focused, focus_note = _focus_spotify_window()
    if not focused:
        return _err(f"Opened Spotify but could not focus its window. {focus_note}")

    try:
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        pyautogui.write(normalized_query, interval=0.02)
        pyautogui.press("enter")
        time.sleep(1.2)
        pyautogui.press("enter")
    except Exception as exc:
        return _err(
            f"Opened Spotify but automatic search/play failed: {exc}. "
            f"Try searching manually for '{normalized_query}'."
        )

    return _ok(
        {
            "opened": True,
            "play_attempted": True,
            "query": normalized_query,
            "window": focus_note,
        },
        f"Opened Spotify and attempted to search/play '{normalized_query}'.",
    )


def get_window_state(window_title: str, max_elements: int = 60) -> dict:
    if not PYWINAUTO_AVAILABLE:
        return _err("pywinauto is not installed.")

    title = str(window_title).strip()
    if not title:
        return _err("Window title cannot be empty.")

    try:
        desktop = PywinautoDesktop(backend="uia")
        windows = [w for w in desktop.windows() if title.lower() in w.window_text().lower()]
        if not windows:
            return _err(f"No window found containing title: {title}")
        window = windows[0]
        controls: list[dict] = []
        for ctrl in window.descendants():
            try:
                info = ctrl.element_info
                ctrl_text = str(info.name or "").strip()
                ctrl_type = str(info.control_type or "").strip()
                if not ctrl_text:
                    continue
                if ctrl_type not in {"Button", "Edit", "MenuItem", "ListItem", "Hyperlink", "TabItem", "CheckBox"}:
                    continue
                controls.append(
                    {
                        "name": ctrl_text[:120],
                        "type": ctrl_type,
                        "automation_id": str(info.automation_id or "")[:80],
                    }
                )
                if len(controls) >= max(10, int(max_elements)):
                    break
            except Exception:
                continue
        return _ok(
            {"window": window.window_text(), "elements": controls, "element_count": len(controls)},
            f"Read {len(controls)} interactive elements from '{window.window_text()}'.",
        )
    except Exception as exc:
        return _err(f"Failed to read window state: {exc}")


def click_in_window(
    window_title: str,
    element_name: str,
    control_type: str | None = None,
    occurrence: int = 1,
) -> dict:
    if not PYWINAUTO_AVAILABLE:
        return _err("pywinauto is not installed.")

    title = str(window_title).strip()
    name = str(element_name).strip()
    if not title or not name:
        return _err("Window title and element name are required.")

    resolved_name, resolved_occurrence = _resolve_ordinal(name, default=occurrence)
    idx = max(0, resolved_occurrence - 1)
    type_filter = str(control_type or "").strip().lower()

    try:
        desktop = PywinautoDesktop(backend="uia")
        windows = [w for w in desktop.windows() if title.lower() in w.window_text().lower()]
        if not windows:
            return _err(f"No window found containing title: {title}")
        window = windows[0]
        candidates = []
        for ctrl in window.descendants():
            try:
                info = ctrl.element_info
                ctrl_text = str(info.name or "").strip()
                ctrl_type = str(info.control_type or "").strip()
                if not ctrl_text:
                    continue
                if type_filter and ctrl_type.lower() != type_filter:
                    continue
                if resolved_name and resolved_name.lower() not in ctrl_text.lower():
                    continue
                candidates.append(ctrl)
            except Exception:
                continue

        if len(candidates) <= idx:
            return _err(f"Element '{element_name}' not found in window '{title}'.")

        target = candidates[idx]
        target.set_focus()
        target.click_input()
        return _ok(
            {"window": window.window_text(), "element": element_name, "occurrence": resolved_occurrence},
            f"Clicked '{element_name}' in '{window.window_text()}'.",
        )
    except Exception as exc:
        return _err(f"Failed to click element in window: {exc}")


def _default_search_roots() -> list[Path]:
    roots: list[Path] = []
    cwd_root = Path.cwd().resolve()
    roots.append(cwd_root)

    anchor = cwd_root.anchor
    if anchor:
        drive_root = Path(anchor)
        if drive_root.exists():
            roots.insert(0, drive_root)

    user_profile = Path(os.environ.get("USERPROFILE", ""))
    for folder in ["Desktop", "Documents", "Downloads"]:
        candidate = user_profile / folder
        if candidate.exists():
            roots.append(candidate)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _tokenize_search_query(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", str(query).lower()):
        token = raw_token.strip("._-")
        if not token:
            continue
        if token in _TEXT_CONTEXT_STOPWORDS and token not in {"ppt", "pdf", "csv", "sql"}:
            continue
        if len(token) < 3 and token not in {"py", "js", "ts", "md", "go", "rs", "c", "h"}:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _infer_extension_hints(tokens: list[str]) -> set[str]:
    hints: set[str] = set()
    for token in tokens:
        if token in {"ppt", "pptx", "slides", "slide", "presentation", "deck"}:
            hints.update({".ppt", ".pptx"})
        elif token in {"pdf", "report", "paper"}:
            hints.add(".pdf")
        elif token in {"notes", "note", "text", "txt", "readme"}:
            hints.update({".txt", ".md", ".markdown"})
        elif token in {"python", "py", "script"}:
            hints.add(".py")
        elif token in {"excel", "sheet", "spreadsheet", "xlsx", "csv"}:
            hints.update({".xlsx", ".xls", ".csv"})
        elif token in {"word", "doc", "docx", "document"}:
            hints.update({".doc", ".docx", ".txt", ".md"})
    return hints


def _score_indexed_file_candidate(
    term: str,
    tokens: list[str],
    extension_hints: set[str],
    filename: str,
    path: str,
    extension: str | None,
    snippet: str | None,
    keywords: str | None,
    summary: str | None,
    modified_ts: float | None,
) -> float:
    filename_lower = filename.lower()
    path_lower = path.lower()
    snippet_lower = str(snippet or "").lower()
    summary_lower = str(summary or "").lower()
    keyword_tokens = {
        part.strip().lower()
        for part in str(keywords or "").split(",")
        if part and part.strip()
    }

    score = 0.0
    if term and term in filename_lower:
        score += 120.0
    if term and term in path_lower:
        score += 90.0
    if term and term in summary_lower:
        score += 55.0
    if term and term in snippet_lower:
        score += 45.0
    if term and term in str(keywords or "").lower():
        score += 60.0

    token_hits = 0
    for token in tokens:
        hit = False
        if token in filename_lower:
            score += 26.0
            hit = True
        if token in path_lower:
            score += 12.0
            hit = True
        if token in keyword_tokens:
            score += 22.0
            hit = True
        elif token in str(keywords or "").lower():
            score += 12.0
            hit = True
        if token in summary_lower:
            score += 12.0
            hit = True
        if token in snippet_lower:
            score += 8.0
            hit = True
        if hit:
            token_hits += 1

    if tokens:
        score += min(30.0, (token_hits / len(tokens)) * 30.0)
        if filename_lower.startswith(tokens[0]):
            score += 10.0

    ext = str(extension or "").lower()
    if extension_hints:
        if ext in extension_hints:
            score += 18.0
        elif ext:
            score -= 2.0

    if modified_ts:
        age_days = max(0.0, (time.time() - float(modified_ts)) / 86400.0)
        if age_days <= 7:
            score += 6.0
        elif age_days <= 30:
            score += 3.0

    return score


def _search_indexed_files(query: str, kind: str = "all", limit: int = 20) -> list[dict]:
    if not _FILE_INDEX_DB.exists():
        return []
    term = query.strip().lower()
    if not term:
        return []
    kinds = {"all", "file", "folder"}
    if kind not in kinds:
        kind = "all"
    if kind == "folder":
        return []
    cap = max(1, int(limit))
    tokens = _tokenize_search_query(term)
    extension_hints = _infer_extension_hints(tokens)

    conn = sqlite3.connect(_FILE_INDEX_DB)
    try:
        search_terms: list[str] = [term]
        for token in tokens:
            if token != term:
                search_terms.append(token)
            if len(search_terms) >= 6:
                break

        where_parts: list[str] = []
        params: list[object] = []
        for search_term in search_terms:
            pattern = f"%{search_term}%"
            where_parts.extend(
                [
                    "lower(filename) LIKE ?",
                    "lower(path) LIKE ?",
                    "lower(COALESCE(content_snippet, '')) LIKE ?",
                    "lower(COALESCE(content_keywords, '')) LIKE ?",
                    "lower(COALESCE(content_summary, '')) LIKE ?",
                ]
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])

        where_clause = " OR ".join(where_parts) if where_parts else "1=0"
        candidate_cap = max(cap * 40, 120)
        try:
            cursor = conn.execute(
                f"""
                SELECT
                    path,
                    filename,
                    extension,
                    modified_ts,
                    content_snippet,
                    content_keywords,
                    content_summary
                FROM files
                WHERE {where_clause}
                ORDER BY modified_ts DESC
                LIMIT ?
                """,
                (*params, candidate_cap),
            )
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc).lower():
                raise
            where_parts = []
            params = []
            for search_term in search_terms:
                pattern = f"%{search_term}%"
                where_parts.extend(["lower(filename) LIKE ?", "lower(path) LIKE ?"])
                params.extend([pattern, pattern])
            where_clause = " OR ".join(where_parts) if where_parts else "1=0"
            cursor = conn.execute(
                f"""
                SELECT path, filename, extension, modified_ts, NULL, NULL, NULL
                FROM files
                WHERE {where_clause}
                ORDER BY modified_ts DESC
                LIMIT ?
                """,
                (*params, candidate_cap),
            )
        rows = cursor.fetchall()
    finally:
        conn.close()

    scored_matches: list[tuple[float, dict]] = []
    seen_paths: set[str] = set()
    for row in rows:
        path_str, filename, extension, modified_ts, snippet, keywords, summary = row
        path_key = str(path_str).lower()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        score = _score_indexed_file_candidate(
            term=term,
            tokens=tokens,
            extension_hints=extension_hints,
            filename=str(filename or ""),
            path=str(path_str),
            extension=str(extension or ""),
            snippet=str(snippet or ""),
            keywords=str(keywords or ""),
            summary=str(summary or ""),
            modified_ts=float(modified_ts) if modified_ts else None,
        )
        if score <= 0:
            continue

        candidate = {
            "type": "file",
            "path": str(path_str),
            "score": round(score, 2),
        }
        if summary:
            candidate["summary"] = str(summary)[:220]
        if keywords:
            keyword_list = [part.strip() for part in str(keywords).split(",") if part.strip()]
            if keyword_list:
                candidate["keywords"] = keyword_list[:6]
        scored_matches.append((score, candidate))

    scored_matches.sort(key=lambda item: (-item[0], len(item[1]["path"])))
    return [candidate for _, candidate in scored_matches[:cap]]


def search_file(query: str, limit: int = 10, open_first: bool = False, kind: str = "all") -> dict:
    term = str(query).strip()
    if not term:
        return _err("Search query cannot be empty.")
    kind_normalized = str(kind).strip().lower()
    if kind_normalized not in {"all", "file", "folder"}:
        kind_normalized = "all"

    matches = _search_indexed_files(term, kind=kind_normalized, limit=limit)
    if not matches:
        return search_local_paths(
            query=term,
            kind=kind_normalized,
            open_first=open_first,
            max_results=max(1, int(limit)),
            max_seconds=8,
        )

    if open_first:
        first = matches[0]
        target = first["path"]
        try:
            if first["type"] == "folder":
                subprocess.Popen(["explorer.exe", target], shell=False)
            else:
                subprocess.Popen(["explorer.exe", "/select,", target], shell=False)
        except Exception as exc:
            return _err(f"Found indexed matches but failed opening first result: {exc}")

    return _ok(
        {"query": term, "kind": kind_normalized, "matches": matches, "source": "index_hybrid"},
        f"Found {len(matches)} indexed matches for '{term}' using hybrid name+context ranking.",
    )


def search_local_paths(
    query: str,
    kind: str = "all",
    open_first: bool = False,
    max_results: int = 20,
    max_seconds: int = 12,
) -> dict:
    term = str(query).strip()
    if not term:
        return _err("Search query cannot be empty.")

    kind_normalized = str(kind).strip().lower()
    if kind_normalized not in {"all", "file", "folder"}:
        return _err("Unsupported search kind. Use all, file, or folder.")

    indexed_matches = _search_indexed_files(term, kind=kind_normalized, limit=max_results)
    if indexed_matches:
        if open_first:
            first = indexed_matches[0]
            target = first["path"]
            try:
                if first["type"] == "folder":
                    subprocess.Popen(["explorer.exe", target], shell=False)
                else:
                    subprocess.Popen(["explorer.exe", "/select,", target], shell=False)
            except Exception as exc:
                return _err(f"Indexed matches found but failed to open first result: {exc}")
        return _ok(
            {"query": term, "kind": kind_normalized, "matches": indexed_matches, "source": "index"},
            f"Found {len(indexed_matches)} indexed matches for '{term}'.",
        )

    roots = _default_search_roots()
    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "Windows",
        "Program Files",
        "Program Files (x86)",
        "ProgramData",
        "AppData",
        "$Recycle.Bin",
        "System Volume Information",
    }
    deadline = time.time() + max(3, int(max_seconds))
    capped_results = max(1, int(max_results))
    term_lower = term.lower()

    matches: list[dict] = []
    timed_out = False

    for root in roots:
        for current, dirs, files in os.walk(root):
            if time.time() > deadline:
                timed_out = True
                break

            dirs[:] = [d for d in dirs if d not in skip_dirs]

            if kind_normalized in {"all", "folder"}:
                for name in dirs:
                    if term_lower in name.lower():
                        matches.append({"type": "folder", "path": str(Path(current) / name)})
                        if len(matches) >= capped_results:
                            break
                if len(matches) >= capped_results:
                    break

            if kind_normalized in {"all", "file"}:
                for name in files:
                    if term_lower in name.lower():
                        matches.append({"type": "file", "path": str(Path(current) / name)})
                        if len(matches) >= capped_results:
                            break
                if len(matches) >= capped_results:
                    break
        if len(matches) >= capped_results or timed_out:
            break

    if not matches:
        if timed_out:
            return _err(f"No local matches found within {max_seconds}s for '{term}'.")
        return _err(f"No local matches found for '{term}'.")

    if open_first:
        first = matches[0]
        target = first["path"]
        try:
            if first["type"] == "folder":
                subprocess.Popen(["explorer.exe", target], shell=False)
            else:
                subprocess.Popen(["explorer.exe", "/select,", target], shell=False)
        except Exception as exc:
            return _err(f"Found matches but failed to open first result: {exc}")

    note = (
        f"Found {len(matches)} local matches for '{term}'"
        + (" (time-limited search)." if timed_out else ".")
    )
    return _ok({"query": term, "kind": kind_normalized, "matches": matches}, note)


def search_in_explorer(query: str, folders_only: bool = True) -> dict:
    term = str(query).strip()
    if not term:
        return _err("Search query cannot be empty.")
    try:
        encoded = quote_plus(term)
        _ = folders_only
        uri = f"search-ms:query={encoded}"
        subprocess.Popen(["explorer.exe", uri], shell=False)
        return _ok({"query": term, "folders_only": folders_only}, f"Opened File Explorer search for '{term}'.")
    except Exception as exc:
        return _err(f"Explorer search failed: {exc}")


def write_in_notepad(
    text: str | None = None,
    force_new: bool = False,
    paste_clipboard: bool = False,
) -> dict:
    if not PYAUTOGUI_AVAILABLE:
        return _err("pyautogui is not installed.")
    if not PYGETWINDOW_AVAILABLE:
        return _err("pygetwindow is not installed.")

    content = str(text or "").strip()
    use_clipboard_paste = bool(paste_clipboard) and not content
    if not content and not use_clipboard_paste:
        return _err("No text provided for Notepad writing.")

    open_result = open_app("notepad", force_new=bool(force_new))
    if open_result["status"] != "success":
        return open_result

    open_payload = open_result.get("result", {})
    used_existing_window = isinstance(open_payload, dict) and open_payload.get("action") == "focused_existing_window"

    window, focus_note = _wait_and_focus_window(["notepad"], timeout_seconds=6.0)
    if window is None:
        return _err(f"Could not focus Notepad window. {focus_note}")

    try:
        time.sleep(0.15)
        if use_clipboard_paste:
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.write(content, interval=0.01)
        action = "focused_existing_window" if used_existing_window else "launched_new_instance"
        content_source = "clipboard" if use_clipboard_paste else "text"
        message = (
            "Focused existing Notepad and pasted clipboard content."
            if used_existing_window and use_clipboard_paste
            else (
                "Opened Notepad and pasted clipboard content."
                if use_clipboard_paste
                else (
                    "Focused existing Notepad and typed requested text."
                    if used_existing_window
                    else "Opened Notepad and typed requested text."
                )
            )
        )
        return _ok(
            {
                "typed": content,
                "window": getattr(window, "title", "Notepad"),
                "action": action,
                "force_new": bool(force_new),
                "source": content_source,
            },
            message,
        )
    except Exception as exc:
        return _err(f"Typing in Notepad failed: {exc}")


def get_running_apps() -> dict:
    if not PYGETWINDOW_AVAILABLE:
        return _err("pygetwindow is not installed.")
    try:
        windows = gw.getAllTitles()
        visible = [title for title in windows if title.strip() and not title.startswith("Default IME")]
        return _ok(visible, f"Found {len(visible)} running windows.")
    except Exception as exc:
        return _err(f"Failed to list running apps: {exc}")


def focus_window(window_title: str) -> dict:
    if not PYGETWINDOW_AVAILABLE:
        return _err("pygetwindow is not installed.")
    try:
        matches = gw.getWindowsWithTitle(window_title)
        if not matches:
            return _err(f"No window found containing title: {window_title}")
        window = matches[0]
        window.activate()
        time.sleep(0.2)
        return _ok({"title": window.title}, f"Focused window: {window.title}")
    except Exception as exc:
        return _err(f"Focus window failed: {exc}")


def type_text(text: str) -> dict:
    if not PYAUTOGUI_AVAILABLE:
        return _err("pyautogui is not installed.")
    try:
        pyautogui.write(text, interval=0.02)
        return _ok({"typed": text}, f"Typed text: {text}")
    except Exception as exc:
        return _err(f"OS typing failed: {exc}")


def press_key(key: str) -> dict:
    if not PYAUTOGUI_AVAILABLE:
        return _err("pyautogui is not installed.")
    try:
        if "+" in key:
            keys = [k.strip() for k in key.split("+") if k.strip()]
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(key)
        return _ok({"key": key}, f"Pressed key: {key}")
    except Exception as exc:
        return _err(f"OS key press failed: {exc}")


def click_at(x: int, y: int) -> dict:
    if not PYAUTOGUI_AVAILABLE:
        return _err("pyautogui is not installed.")
    try:
        pyautogui.click(int(x), int(y))
        return _ok({"x": int(x), "y": int(y)}, f"Clicked at ({int(x)}, {int(y)})")
    except Exception as exc:
        return _err(f"OS click failed: {exc}")


def take_screenshot(filename: str | None = None) -> dict:
    if not PYAUTOGUI_AVAILABLE:
        return _err("pyautogui is not installed.")
    try:
        workspace = _workspace_root()
        name = filename if filename else f"screenshot_{int(time.time())}.png"
        target = _resolve_workspace_path(name, allow_missing_parent=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        image = pyautogui.screenshot()
        image.save(target)
        return _ok({"filepath": str(target)}, f"Screenshot saved to {target}")
    except Exception as exc:
        return _err(f"Screenshot failed: {exc}")


def run_shell_command(command: str, shell: str = "powershell") -> dict:
    blocked_patterns = ["format ", "del /f", "rm -rf", "rmdir /s", "shutdown", "reg delete"]
    lower_command = command.lower()
    for pattern in blocked_patterns:
        if pattern in lower_command:
            return _err(f"Command blocked for safety: contains '{pattern}'.")
    try:
        if shell.strip().lower() == "powershell":
            proc = subprocess.run(
                ["powershell", "-Command", command],
                capture_output=True,
                text=True,
                timeout=15,
            )
        else:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            return _err(f"Command failed (exit {proc.returncode}): {output[:500]}")
        return _ok({"output": output[:2000], "returncode": proc.returncode}, "Command executed successfully.")
    except subprocess.TimeoutExpired:
        return _err("Command timed out after 15 seconds.")
    except Exception as exc:
        return _err(f"Shell execution failed: {exc}")


def get_system_health_snapshot() -> dict:
    try:
        snapshot = _call_system_monitor("get_health_snapshot")
        return _ok(snapshot, "Captured system health snapshot.")
    except Exception as exc:
        return _err(f"Health snapshot failed: {exc}")


def list_running_processes(limit: int = 100) -> dict:
    try:
        processes = _call_system_monitor("list_running_processes", limit=limit)
        return _ok(processes, f"Found {len(processes)} running processes.")
    except Exception as exc:
        return _err(f"Process listing failed: {exc}")


def get_network_status() -> dict:
    try:
        status = _call_system_monitor("get_network_status")
        adapter_count = len(status.get("adapters", [])) if isinstance(status, dict) else 0
        return _ok(status, f"Captured network status ({adapter_count} adapters).")
    except Exception as exc:
        return _err(f"Network status failed: {exc}")


def list_usb_devices() -> dict:
    try:
        devices = _call_system_monitor("list_usb_devices")
        return _ok(devices, f"Found {len(devices)} USB devices.")
    except Exception as exc:
        return _err(f"USB listing failed: {exc}")


def run_powershell_command(command: str, timeout_seconds: int = 20, human_approval: bool = False) -> dict:
    _ = human_approval
    try:
        result = _call_system_monitor(
            "execute_powershell",
            command=command,
            timeout_seconds=timeout_seconds,
        )
        return _ok(result, "PowerShell command executed successfully.")
    except Exception as exc:
        return _err(f"PowerShell execution failed: {exc}")


def kill_process(
    pid: int | None = None,
    process_name: str | None = None,
    force: bool = True,
    human_approval: bool = False,
) -> dict:
    _ = human_approval
    try:
        result = _call_system_monitor("kill_process", pid=pid, process_name=process_name, force=force)
        if result.get("pid") is not None:
            target = f"PID {result['pid']}"
        else:
            target = str(result.get("process_name") or "unknown process")
        return _ok(result, f"Killed process target: {target}.")
    except Exception as exc:
        return _err(f"Process kill failed: {exc}")


def get_usb_devices() -> dict:
    return list_usb_devices()


def disable_usb_device(device_id: str, human_approval: bool = False) -> dict:
    _ = human_approval
    target = str(device_id).strip()
    if not target:
        return _err("Device ID is required.")
    try:
        proc = subprocess.run(
            ["pnputil", "/disable-device", target],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            return _err(f"USB disable failed (exit {proc.returncode}): {output[:500]}")
        return _ok({"device_id": target, "output": output[:500]}, "USB device disable command executed.")
    except Exception as exc:
        return _err(f"Failed to disable USB device: {exc}")


def get_firewall_rules(limit_chars: int = 12000) -> dict:
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            return _err(f"Failed to read firewall rules (exit {proc.returncode}).")
        cap = max(1000, int(limit_chars))
        return _ok({"rules_text": output[:cap]}, "Fetched Windows firewall rules.")
    except Exception as exc:
        return _err(f"Failed to fetch firewall rules: {exc}")


def add_firewall_rule(
    name: str,
    direction: str,
    action: str,
    port: int,
    human_approval: bool = False,
) -> dict:
    _ = human_approval
    rule_name = str(name).strip()
    if not rule_name:
        return _err("Firewall rule name is required.")
    dir_value = str(direction).strip().lower()
    action_value = str(action).strip().lower()
    if dir_value not in {"in", "out"}:
        return _err("Direction must be 'in' or 'out'.")
    if action_value not in {"allow", "block"}:
        return _err("Action must be 'allow' or 'block'.")
    try:
        local_port = int(port)
    except Exception:
        return _err("Port must be an integer.")

    try:
        proc = subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={rule_name}",
                f"dir={dir_value}",
                f"action={action_value}",
                "protocol=TCP",
                f"localport={local_port}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            return _err(f"Firewall rule creation failed (exit {proc.returncode}): {output[:500]}")
        return _ok(
            {"name": rule_name, "direction": dir_value, "action": action_value, "port": local_port},
            f"Firewall rule '{rule_name}' added.",
        )
    except Exception as exc:
        return _err(f"Failed to add firewall rule: {exc}")


def read_registry(hive: str, path: str, key: str) -> dict:
    try:
        import winreg
    except ImportError:
        return _err("winreg is unavailable on this platform.")

    hive_map = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
    }
    hive_key = hive_map.get(str(hive).strip().upper())
    if hive_key is None:
        return _err("Unsupported hive. Use HKLM or HKCU.")

    target_path = str(path).strip()
    value_key = str(key).strip()
    if not target_path or not value_key:
        return _err("Registry path and key are required.")

    try:
        reg_key = winreg.OpenKey(hive_key, target_path)
        value, value_type = winreg.QueryValueEx(reg_key, value_key)
        return _ok(
            {"hive": str(hive).upper(), "path": target_path, "key": value_key, "value": value, "type": value_type},
            "Read registry value successfully.",
        )
    except Exception as exc:
        return _err(f"Registry read failed: {exc}")


def run_command(command: str, human_approval: bool = False) -> dict:
    return run_powershell_command(command=command, timeout_seconds=20, human_approval=human_approval)


def mute_audio(mute: bool = True) -> dict:
    global _mute_state
    try:
        if not PYAUTOGUI_AVAILABLE:
            return _err("pyautogui is not installed.")
        if _mute_state is None:
            pyautogui.press("volumemute")
            _mute_state = mute
            return _ok({"muted": _mute_state}, "Toggled system mute (initial state inferred).")
        if mute != _mute_state:
            pyautogui.press("volumemute")
            _mute_state = mute
        return _ok({"muted": _mute_state}, f"System audio mute state requested: {mute}")
    except Exception as exc:
        return _err(f"Mute control failed: {exc}")


def read_file(path: str) -> dict:
    try:
        target = _resolve_workspace_path(path)
        if not target.is_file():
            return _err(f"Path is not a file: {target}")
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return _ok({"path": str(target), "content": content[:5000]}, f"Read {len(content)} chars from {target}")
    except Exception as exc:
        return _err(f"Read failed: {exc}")


def write_file(path: str, content: str) -> dict:
    try:
        target = _resolve_workspace_path(path, allow_missing_parent=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return _ok({"path": str(target), "bytes": len(content)}, f"Wrote {len(content)} chars to {target}")
    except Exception as exc:
        return _err(f"Write failed: {exc}")


def list_files(directory: str = "workspace", depth: int = 2) -> dict:
    try:
        if directory == "workspace":
            root = _workspace_root()
        else:
            root = _resolve_workspace_path(directory)
            if not root.is_dir():
                return _err(f"Path is not a directory: {root}")
        max_depth = max(0, int(depth))

        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        items: list[dict] = []
        while queue:
            current, current_depth = queue.popleft()
            try:
                entries = sorted(current.iterdir(), key=lambda entry: (entry.is_file(), entry.name.lower()))
            except PermissionError:
                continue
            for entry in entries:
                rel_path = str(entry.relative_to(root))
                if entry.is_dir():
                    items.append({"path": rel_path + "\\", "type": "dir"})
                    if current_depth < max_depth:
                        queue.append((entry, current_depth + 1))
                else:
                    items.append(
                        {
                            "path": rel_path,
                            "type": "file",
                            "size_bytes": entry.stat().st_size,
                        }
                    )
        return _ok(items, f"Found {len(items)} entries in {root} (BFS depth={max_depth}).")
    except Exception as exc:
        return _err(f"List files failed: {exc}")


def update_user_profile(key: str, value: str) -> dict:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return _err("Profile key cannot be empty.")

    sensitive = _is_sensitive_profile_key(normalized_key)
    display_value = _format_profile_value_for_display(value, sensitive=sensitive)
    try:
        profile = load_user_profile_dict()
        profile[normalized_key] = str(value)
        save_user_profile_dict(profile)
        return _ok(
            {
                "key": normalized_key,
                "value": display_value,
                "sensitive": sensitive,
            },
            f"Updated user profile: {normalized_key} = {display_value}",
        )
    except SecureMemoryError as exc:
        return _err(f"Profile update failed: {exc}")
    except Exception as exc:
        return _err(f"Profile update failed: {exc}")


def report_failure(reason: str) -> dict:
    return {"status": "failure", "result": {"reason": reason}, "message": f"Task cannot be completed: {reason}"}


def _infer_tool_schema(tool_fn) -> dict[str, list[str]]:
    required_args: list[str] = []
    optional_args: list[str] = []
    try:
        signature = inspect.signature(tool_fn)
    except (TypeError, ValueError):
        return {"required_args": required_args, "optional_args": optional_args}

    for name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.default is inspect.Parameter.empty:
            required_args.append(name)
        else:
            optional_args.append(name)
    return {"required_args": required_args, "optional_args": optional_args}


def get_tool_contracts() -> dict:
    """Return strict tool contract metadata for router/executor use."""
    contracts: dict[str, dict] = {}
    for tool_name, tool_info in TOOL_REGISTRY.items():
        tool_fn = tool_info.get("fn")
        schema = _infer_tool_schema(tool_fn)
        contracts[tool_name] = {
            "description": str(tool_info.get("description", "")).strip(),
            "required_args": schema["required_args"],
            "optional_args": schema["optional_args"],
            "requires_approval": bool(tool_info.get("requires_approval")),
        }
    return _ok(contracts, f"Generated strict contracts for {len(contracts)} tools.")


TOOL_REGISTRY = {
    "browser_navigate": {
        "fn": browser_navigate,
        "description": "Navigate/open browser to URL and return accessibility-based page state.",
        "args": {"url": "string", "browser": "string (optional)"},
    },
    "browser_get_state": {
        "fn": browser_get_state,
        "description": "Read structured browser accessibility state for closed-loop decisions.",
        "args": {
            "max_elements": "integer (optional)",
            "text_query": "string (optional)",
            "text_limit": "integer (optional)",
            "copy_to_clipboard": "boolean (optional)",
        },
    },
    "get_page_title": {
        "fn": get_page_title,
        "description": "Read the title of the active browser page.",
        "args": {},
    },
    "copy_text_to_clipboard": {
        "fn": copy_text_to_clipboard,
        "description": "Copy explicit text to the system clipboard.",
        "args": {"text": "string"},
    },
    "browser_switch_profile": {
        "fn": browser_switch_profile,
        "description": (
            "Switch browser profile mode (default/snapshot/automation) and optional browser target "
            "with deterministic relaunch behavior."
        ),
        "args": {
            "profile_mode": "string (optional: default/snapshot/automation)",
            "browser": "string (optional: chrome/edge/firefox/chromium)",
            "relaunch": "boolean (optional, default true)",
        },
    },
    "browser_type": {
        "fn": browser_type,
        "description": "Type text into browser input by accessible element name or active field.",
        "args": {
            "text": "string",
            "element_name": "string (optional)",
            "clear": "boolean (optional)",
            "multiline": "boolean (optional)",
            "newline_mode": "string (optional: shift_enter/enter/literal)",
            "submit": "boolean (optional)",
        },
    },
    "browser_press_key": {
        "fn": browser_press_key,
        "description": "Press keyboard key in active browser page and return updated state.",
        "args": {"key": "string"},
    },
    "browser_click": {
        "fn": browser_click,
        "description": "Click browser element by accessible name with optional role and occurrence.",
        "args": {
            "element_name": "string",
            "role": "string (optional)",
            "occurrence": "integer (optional)",
        },
    },
    "open_browser": {
        "fn": open_browser,
        "description": "Backward-compatible alias for browser_navigate.",
        "args": {"url": "string (optional)", "browser": "string (optional)"},
    },
    "navigate_to": {
        "fn": navigate_to,
        "description": "Backward-compatible alias for browser_navigate.",
        "args": {"url": "string"},
    },
    "search_youtube": {
        "fn": search_youtube,
        "description": "Search YouTube and return the top result title and URL.",
        "args": {"query": "string"},
    },
    "click_youtube_first_result": {
        "fn": click_youtube_first_result,
        "description": "Click the first video result on a YouTube search page.",
        "args": {},
    },
    "browser_stress_50_sites": {
        "fn": browser_stress_50_sites,
        "description": (
            "Deterministic high-volume browser stress run for up to 50+ sites with retries, per-site telemetry, "
            "and explicit failure signaling when sites are unavailable."
        ),
        "args": {
            "sites": "string (optional comma/newline-separated URLs)",
            "site_count": "integer (optional, default 50)",
            "retries": "integer (optional, default 1)",
            "timeout_seconds": "integer (optional, default 12)",
            "wait_after_load_ms": "integer (optional, default 80)",
            "browser": "string (optional: chrome/edge/firefox/chromium)",
            "require_http_ok": "boolean (optional, default true)",
            "open_each_in_new_tab": "boolean (optional, default true)",
            "dry_run": "boolean (optional, default false)",
        },
    },
    "youtube_comment_pipeline": {
        "fn": youtube_comment_pipeline,
        "description": (
            "Search YouTube, open first result, pause playback, extract comments, and export to Desktop text file "
            "with strict prerequisite checks."
        ),
        "args": {
            "query": "string",
            "comment_count": "integer (optional, default 20)",
            "output_filename": "string (optional, .txt name on Desktop)",
            "open_in_notepad": "boolean (optional, default false)",
            "pause_after_seconds": "integer (optional, default 2)",
            "browser": "string (optional: chrome/edge/firefox/chromium)",
            "dry_run": "boolean (optional, default false)",
        },
    },
    "web_search": {
        "fn": web_search,
        "description": "Run a web search and return top results.",
        "args": {"query": "string", "engine": "string (optional: google/bing)"},
    },
    "type_in_browser": {
        "fn": type_in_browser,
        "description": "Backward-compatible browser typing (selector or active input).",
        "args": {
            "text": "string",
            "selector": "string (optional)",
            "multiline": "boolean (optional)",
            "newline_mode": "string (optional: shift_enter/enter/literal)",
            "submit": "boolean (optional)",
        },
    },
    "press_key_in_browser": {
        "fn": press_key_in_browser,
        "description": "Backward-compatible alias for browser_press_key.",
        "args": {"key": "string"},
    },
    "click_in_browser": {
        "fn": click_in_browser,
        "description": "Backward-compatible browser click (selector or visible text).",
        "args": {"selector": "string (optional)", "text": "string (optional)"},
    },
    "get_page_content": {
        "fn": get_page_content,
        "description": "Get visible text content from the active browser page.",
        "args": {},
    },
    "check_app_availability": {
        "fn": check_app_availability,
        "description": "Check whether an app is installed/launchable on this PC.",
        "args": {"app_name": "string"},
    },
    "open_app": {
        "fn": open_app,
        "description": "Open/focus a Windows app by name/executable, reusing an existing window by default.",
        "args": {"app_name": "string", "force_new": "boolean (optional, default false)"},
    },
    "check_file_handler": {
        "fn": check_file_handler,
        "description": "Check whether a file extension has a launchable default Windows handler.",
        "args": {"extension": "string (e.g., .pdf, .pptx)"},
    },
    "open_file_with_default_app": {
        "fn": open_file_with_default_app,
        "description": "Open a local file path using the default associated Windows app.",
        "args": {"path": "string"},
    },
    "open_extension_handler": {
        "fn": open_extension_handler,
        "description": "Open the default handler app for a file extension (e.g., .pdf/.pptx).",
        "args": {"extension": "string (e.g., .pdf, .pptx)"},
    },
    "open_existing_document": {
        "fn": open_existing_document,
        "description": (
            "Find and open an existing local PDF/PPT/PPTX file, preferring indexed results and falling back "
            "to live desktop/document scans."
        ),
        "args": {
            "extension": "string (e.g., .pdf, .ppt, .pptx)",
            "query": "string (optional filename/path hint)",
            "limit": "integer (optional, default 20)",
        },
    },
    "spotify_play": {
        "fn": spotify_play,
        "description": "Open Spotify and attempt automated search/play for a track query.",
        "args": {"query": "string (optional)"},
    },
    "index_files": {
        "fn": index_files,
        "description": "Build/refresh local file index database for fast PC search.",
        "args": {"scope": "string (optional: quick/full)", "max_files": "integer (optional)"},
    },
    "index_apps": {
        "fn": index_apps,
        "description": "Build/refresh installed applications index for open_app resolution.",
        "args": {},
    },
    "search_file": {
        "fn": search_file,
        "description": "Search files/folders using index first, fallback to live scan.",
        "args": {
            "query": "string",
            "limit": "integer (optional)",
            "open_first": "boolean (optional)",
            "kind": "string (optional: all/file/folder)",
        },
    },
    "search_local_paths": {
        "fn": search_local_paths,
        "description": "Search local PC paths for file/folder names and optionally open first result.",
        "args": {
            "query": "string",
            "kind": "string (optional: all/file/folder)",
            "open_first": "boolean (optional)",
            "max_results": "integer (optional)",
            "max_seconds": "integer (optional)",
        },
    },
    "search_in_explorer": {
        "fn": search_in_explorer,
        "description": "Open File Explorer search for files/folders matching a query.",
        "args": {"query": "string", "folders_only": "boolean (optional)"},
    },
    "write_in_notepad": {
        "fn": write_in_notepad,
        "description": "Open/focus Notepad and type text or paste clipboard content.",
        "args": {
            "text": "string (optional)",
            "force_new": "boolean (optional, default false)",
            "paste_clipboard": "boolean (optional, default false)",
        },
    },
    "get_running_apps": {
        "fn": get_running_apps,
        "description": "List active window titles.",
        "args": {},
    },
    "get_window_state": {
        "fn": get_window_state,
        "description": "Read interactive controls of an open application window using UI Automation.",
        "args": {"window_title": "string", "max_elements": "integer (optional)"},
    },
    "click_in_window": {
        "fn": click_in_window,
        "description": "Click a named control in an open window using UI Automation.",
        "args": {
            "window_title": "string",
            "element_name": "string",
            "control_type": "string (optional)",
            "occurrence": "integer (optional)",
        },
    },
    "focus_window": {
        "fn": focus_window,
        "description": "Bring a window to foreground by partial title.",
        "args": {"window_title": "string"},
    },
    "type_text": {
        "fn": type_text,
        "description": "Type text at current OS focus.",
        "args": {"text": "string"},
    },
    "press_key": {
        "fn": press_key,
        "description": "Press an OS key or hotkey (example: ctrl+c).",
        "args": {"key": "string"},
    },
    "click_at": {
        "fn": click_at,
        "description": "Click at screen coordinates (last resort).",
        "args": {"x": "integer", "y": "integer"},
    },
    "take_screenshot": {
        "fn": take_screenshot,
        "description": "Capture a screenshot into the workspace.",
        "args": {"filename": "string (optional)"},
    },
    "get_system_health_snapshot": {
        "fn": get_system_health_snapshot,
        "description": "Capture a system health snapshot (CPU/memory/disk/uptime/process count).",
        "args": {},
    },
    "list_running_processes": {
        "fn": list_running_processes,
        "description": "List currently running OS processes with PID and memory usage.",
        "args": {"limit": "integer (optional, default 100)"},
    },
    "get_network_status": {
        "fn": get_network_status,
        "description": "Read current network adapter, IP, and profile status.",
        "args": {},
    },
    "list_usb_devices": {
        "fn": list_usb_devices,
        "description": "List currently connected USB devices.",
        "args": {},
    },
    "run_shell_command": {
        "fn": run_shell_command,
        "description": "Run a shell command and return output.",
        "args": {"command": "string", "shell": "string (optional: powershell/cmd)"},
    },
    "run_powershell_command": {
        "fn": run_powershell_command,
        "description": "Run a PowerShell command with explicit human approval.",
        "args": {
            "command": "string",
            "timeout_seconds": "integer (optional, default 20)",
            "human_approval": "boolean (required true)",
        },
        "requires_approval": True,
    },
    "run_command": {
        "fn": run_command,
        "description": "Run a PowerShell command with explicit human approval.",
        "args": {"command": "string", "human_approval": "boolean (required true)"},
        "requires_approval": True,
    },
    "get_usb_devices": {
        "fn": get_usb_devices,
        "description": "List connected USB devices.",
        "args": {},
    },
    "disable_usb_device": {
        "fn": disable_usb_device,
        "description": "Disable a USB device by ID (approval required).",
        "args": {"device_id": "string", "human_approval": "boolean (required true)"},
        "requires_approval": True,
    },
    "kill_process": {
        "fn": kill_process,
        "description": "Kill a running process by PID or process name (approval required).",
        "args": {
            "pid": "integer (optional)",
            "process_name": "string (optional)",
            "force": "boolean (optional, default true)",
            "human_approval": "boolean (required true)",
        },
        "requires_approval": True,
    },
    "get_firewall_rules": {
        "fn": get_firewall_rules,
        "description": "List Windows firewall rules.",
        "args": {"limit_chars": "integer (optional)"},
    },
    "add_firewall_rule": {
        "fn": add_firewall_rule,
        "description": "Add a Windows firewall rule (approval required).",
        "args": {
            "name": "string",
            "direction": "string (in/out)",
            "action": "string (allow/block)",
            "port": "integer",
            "human_approval": "boolean (required true)",
        },
        "requires_approval": True,
    },
    "read_registry": {
        "fn": read_registry,
        "description": "Read a registry value from HKLM/HKCU.",
        "args": {"hive": "string", "path": "string", "key": "string"},
    },
    "mute_audio": {
        "fn": mute_audio,
        "description": "Mute or unmute system audio.",
        "args": {"mute": "boolean (true/false)"},
    },
    "read_file": {
        "fn": read_file,
        "description": "Read a file inside the workspace.",
        "args": {"path": "string"},
    },
    "write_file": {
        "fn": write_file,
        "description": "Write a file inside the workspace.",
        "args": {"path": "string", "content": "string"},
    },
    "save_text_to_desktop_file": {
        "fn": save_text_to_desktop_file,
        "description": "Write text to a Desktop .txt file and optionally open it in Notepad.",
        "args": {
            "content": "string (optional)",
            "filename": "string (optional)",
            "open_in_notepad": "boolean (optional, default false)",
            "from_clipboard": "boolean (optional, default false)",
        },
    },
    "web_codegen_autofix": {
        "fn": web_codegen_autofix,
        "description": (
            "Generate Python code from ChatGPT browser/configured web-assistant/free-AI path, write file, execute, "
            "auto-fix runtime failures iteratively, and return concrete diagnostics on unresolved runs."
        ),
        "args": {
            "request": "string",
            "filename": "string (optional, default generated_script.py)",
            "max_fix_rounds": "integer (optional, default 2)",
            "run_timeout_seconds": "integer (optional, default 20)",
            "dry_run": "boolean (optional, default false)",
        },
    },
    "list_files": {
        "fn": list_files,
        "description": "List files/dirs in BFS order inside workspace.",
        "args": {"directory": "string (optional)", "depth": "integer (optional)"},
    },
    "update_user_profile": {
        "fn": update_user_profile,
        "description": "Update one user profile field in secure vault storage.",
        "args": {"key": "string", "value": "string"},
    },
    "get_tool_contracts": {
        "fn": get_tool_contracts,
        "description": "Return strict tool schema contracts including required/optional args.",
        "args": {},
    },
    "report_failure": {
        "fn": report_failure,
        "description": "Signal that a task cannot be completed with available tools.",
        "args": {"reason": "string"},
    },
}


def tool_requires_approval(name: str) -> bool:
    tool_info = TOOL_REGISTRY.get(name)
    return bool(tool_info and tool_info.get("requires_approval"))


def dispatch_tool(name: str, args: dict) -> dict:
    """Dispatch a registered tool by name and return its structured result."""
    if name not in TOOL_REGISTRY:
        return _err(f"Unknown tool: '{name}'. Available: {', '.join(TOOL_REGISTRY.keys())}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _err("Tool arguments must be a JSON object.")

    cleaned_args = {}
    for key, value in args.items():
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "false"}:
                cleaned_args[key] = lowered == "true"
                continue
            if lowered.isdigit():
                cleaned_args[key] = int(lowered)
                continue
            try:
                cleaned_args[key] = float(lowered)
                continue
            except ValueError:
                pass
        cleaned_args[key] = value

    tool_fn = TOOL_REGISTRY[name]["fn"]
    try:
        result = tool_fn(**cleaned_args)
    except TypeError as exc:
        return _err(f"Wrong arguments for '{name}': {exc}")
    except Exception as exc:
        return _err(f"Tool execution error in '{name}': {exc}")

    if not isinstance(result, dict) or "status" not in result:
        return _err(f"Tool '{name}' returned an invalid result payload.")
    return result
