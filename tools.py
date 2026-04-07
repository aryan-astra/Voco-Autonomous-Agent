"""
VOCO Tools - Windows OS automation tool library.
Every tool returns:
{"status": "success" | "error" | "failure", "result": any, "message": str}
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from constants import USER_PROFILE_FILE, WORKSPACE_PATH

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
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


_playwright_instance = None
_browser_instance = None
_page_instance = None
_mute_state: bool | None = None


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


def _get_browser_page():
    global _playwright_instance, _browser_instance, _page_instance
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        )

    if _page_instance is None or _page_instance.is_closed():
        if _playwright_instance is None:
            _playwright_instance = sync_playwright().start()
        if _browser_instance is None or not _browser_instance.is_connected():
            _browser_instance = _playwright_instance.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )
        _page_instance = _browser_instance.new_page()
    return _page_instance


def open_browser(url: str = "https://www.google.com", browser: str = "chromium") -> dict:
    _ = browser
    try:
        page = _get_browser_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return _ok(
            {"url": page.url, "title": page.title()},
            f"Opened browser at {page.url}",
        )
    except PlaywrightTimeoutError:
        return _err(f"Browser navigation timed out for URL: {url}")
    except Exception as exc:
        return _err(f"Browser open failed: {exc}")


def navigate_to(url: str) -> dict:
    try:
        page = _get_browser_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return _ok(
            {"url": page.url, "title": page.title()},
            f"Navigated to {page.url}",
        )
    except PlaywrightTimeoutError:
        return _err(f"Navigation timed out for URL: {url}")
    except Exception as exc:
        return _err(f"Navigation failed: {exc}")


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


def type_in_browser(text: str, selector: str | None = None) -> dict:
    try:
        page = _get_browser_page()
        if selector:
            target = page.wait_for_selector(selector, timeout=5000)
            target.fill(text)
        else:
            page.keyboard.type(text)
        return _ok({"typed": text, "selector": selector}, f"Typed text in browser: {text}")
    except PlaywrightTimeoutError:
        return _err(f"Browser selector not found: {selector}")
    except Exception as exc:
        return _err(f"Browser typing failed: {exc}")


def press_key_in_browser(key: str) -> dict:
    try:
        page = _get_browser_page()
        page.keyboard.press(key)
        return _ok({"key": key}, f"Pressed browser key: {key}")
    except Exception as exc:
        return _err(f"Browser key press failed: {exc}")


def get_page_content() -> dict:
    try:
        page = _get_browser_page()
        content = page.inner_text("body")[:3000]
        return _ok({"url": page.url, "content": content}, f"Captured page text from {page.url}")
    except Exception as exc:
        return _err(f"Failed to capture page content: {exc}")


def open_app(app_name: str) -> dict:
    app_map = {
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
    }
    executable = app_map.get(app_name.strip().lower(), app_name)
    try:
        if executable.startswith("ms-"):
            subprocess.Popen(["start", "", executable], shell=True)
        else:
            subprocess.Popen(executable, shell=True)
        time.sleep(1.0)
        return _ok({"app": app_name, "executable": executable}, f"Opened application: {app_name}")
    except FileNotFoundError:
        return _err(f"Application not found: {app_name}")
    except Exception as exc:
        return _err(f"Failed to open app '{app_name}': {exc}")


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
    try:
        profile_path = Path(USER_PROFILE_FILE)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile = {}
        if profile_path.exists():
            with open(profile_path, "r", encoding="utf-8") as f:
                existing = f.read()
            loaded = yaml.safe_load(existing) if existing.strip() else {}
            if isinstance(loaded, dict):
                profile = loaded
        profile[key] = value
        with open(profile_path, "w", encoding="utf-8") as f:
            yaml.dump(profile, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
        return _ok({key: value}, f"Updated user profile: {key} = {value}")
    except Exception as exc:
        return _err(f"Profile update failed: {exc}")


def report_failure(reason: str) -> dict:
    return {"status": "failure", "result": {"reason": reason}, "message": f"Task cannot be completed: {reason}"}


TOOL_REGISTRY = {
    "open_browser": {
        "fn": open_browser,
        "description": "Open a browser and navigate to a URL.",
        "args": {"url": "string (optional)", "browser": "string (optional)"},
    },
    "navigate_to": {
        "fn": navigate_to,
        "description": "Navigate the active browser page to a URL.",
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
    "web_search": {
        "fn": web_search,
        "description": "Run a web search and return top results.",
        "args": {"query": "string", "engine": "string (optional: google/bing)"},
    },
    "type_in_browser": {
        "fn": type_in_browser,
        "description": "Type text in browser focus or a CSS selector target.",
        "args": {"text": "string", "selector": "string (optional)"},
    },
    "press_key_in_browser": {
        "fn": press_key_in_browser,
        "description": "Press a keyboard key in the active browser page.",
        "args": {"key": "string"},
    },
    "get_page_content": {
        "fn": get_page_content,
        "description": "Get visible text content from the active browser page.",
        "args": {},
    },
    "open_app": {
        "fn": open_app,
        "description": "Open a Windows desktop app by common name or executable.",
        "args": {"app_name": "string"},
    },
    "get_running_apps": {
        "fn": get_running_apps,
        "description": "List active window titles.",
        "args": {},
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
    "run_shell_command": {
        "fn": run_shell_command,
        "description": "Run a shell command and return output.",
        "args": {"command": "string", "shell": "string (optional: powershell/cmd)"},
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
    "list_files": {
        "fn": list_files,
        "description": "List files/dirs in BFS order inside workspace.",
        "args": {"directory": "string (optional)", "depth": "integer (optional)"},
    },
    "update_user_profile": {
        "fn": update_user_profile,
        "description": "Update one user profile field in USER.yaml.",
        "args": {"key": "string", "value": "string"},
    },
    "report_failure": {
        "fn": report_failure,
        "description": "Signal that a task cannot be completed with available tools.",
        "args": {"reason": "string"},
    },
}


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
