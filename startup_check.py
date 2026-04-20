"""VOCO startup diagnostics and lightweight environment bootstrap."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import constants


PACKAGE_REQUIREMENTS: dict[str, str] = {
    "faster_whisper": "faster-whisper",
    "sounddevice": "sounddevice",
    "numpy": "numpy",
    "playwright": "playwright",
    "requests": "requests",
    "pywinauto": "pywinauto",
    "wmi": "wmi",
    "watchdog": "watchdog",
    "sklearn": "scikit-learn",
}

_STARTUP_RAN = False


def _check_python_version(issues: list[str], fatals: list[str]) -> None:
    if sys.version_info >= (3, 10):
        return
    fatals.append("Python 3.10+ is required.")
    issues.append(f"Python version is {sys.version.split()[0]} (requires >= 3.10).")


def _check_packages(issues: list[str]) -> None:
    for module_name, pip_name in PACKAGE_REQUIREMENTS.items():
        try:
            importlib.import_module(module_name)
        except Exception:
            issues.append(f"Missing package '{pip_name}' (import '{module_name}' failed).")


def _fetch_ollama_tags(timeout_seconds: int = 3) -> dict | None:
    tags_url = f"{constants.OLLAMA_URL}/api/tags"
    try:
        with urlopen(tags_url, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
        import json

        payload = json.loads(body)
        if isinstance(payload, dict):
            return payload
    except (URLError, TimeoutError, ValueError):
        return None
    return None


def _ensure_ollama_running(issues: list[str], fatals: list[str], *, autofix: bool) -> dict | None:
    payload = _fetch_ollama_tags(timeout_seconds=3)
    if payload is not None:
        return payload

    if not autofix:
        fatals.append("Ollama is not reachable.")
        return None

    try:
        subprocess.Popen(["ollama", "serve"])  # noqa: S603
        time.sleep(5)
    except (FileNotFoundError, OSError):
        fatals.append("Ollama executable was not found. Install Ollama and retry.")
        return None

    payload = _fetch_ollama_tags(timeout_seconds=3)
    if payload is None:
        fatals.append("Ollama is not reachable after startup attempt.")
        return None

    issues.append("Ollama was not running and was started automatically.")
    return payload


def _check_model_registered(tags_payload: dict | None, issues: list[str]) -> None:
    if not isinstance(tags_payload, dict):
        return
    models = tags_payload.get("models", [])
    installed_names: list[str] = []
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if name:
                    installed_names.append(name)
    target = constants.OLLAMA_MODEL
    is_installed = any(name == target or name.startswith(f"{target}:") for name in installed_names)
    if not is_installed:
        issues.append(f"Ollama model '{target}' is missing. Run: ollama pull {target}")

    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=8)  # noqa: S603
        if result.returncode == 0 and "voco-agent" not in result.stdout:
            modelfile = constants.BASE_DIR / "models" / "voco_model.Modelfile"
            issues.append(
                f"Model 'voco-agent' is not registered. Run: ollama create voco-agent -f \"{modelfile}\""
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        issues.append("Could not verify 'voco-agent' registration via `ollama list`.")


def _detect_gpu_layers() -> int:
    override = os.environ.get(constants.GPU_LAYERS_ENV, "").strip()
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            pass

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
        )  # noqa: S603
        if result.returncode == 0 and str(result.stdout).strip():
            return 999
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.LoadLibrary("OpenCL.dll")
            return 1
        except OSError:
            pass

    return constants.GPU_LAYERS


def _ensure_sample_search_space(issues: list[str], *, autofix: bool) -> None:
    sample_root = constants.SAMPLE_SEARCH_SPACE
    if sample_root.exists():
        return
    if not autofix:
        issues.append(f"Sample search root is missing: {sample_root}")
        return
    sample_root.mkdir(parents=True, exist_ok=True)
    readme_path = sample_root / "README.md"
    if not readme_path.exists():
        readme_path.write_text(
            "# VOCO Sample Search Space\n\nPlace demo files here for quick indexing.\n",
            encoding="utf-8",
        )
    issues.append(f"Created sample search root: {sample_root}")


def run_startup_check(*, autofix: bool = True, strict: bool = False) -> dict[str, object]:
    """Run startup checks once per process and return a structured status payload."""
    global _STARTUP_RAN
    if _STARTUP_RAN:
        return {"ok": True, "issues": [], "fatal_issues": [], "already_ran": True}

    issues: list[str] = []
    fatal_issues: list[str] = []
    _check_python_version(issues, fatal_issues)
    _check_packages(issues)
    tags_payload = _ensure_ollama_running(issues, fatal_issues, autofix=autofix)
    _check_model_registered(tags_payload, issues)
    _ensure_sample_search_space(issues, autofix=autofix)

    gpu_layers = _detect_gpu_layers()
    os.environ.setdefault(constants.GPU_LAYERS_ENV, str(gpu_layers))

    ok = len(fatal_issues) == 0
    summary = f"VOCO startup check: {'OK' if ok else 'FAILED'} ({len(issues)} issues)"
    print(summary)
    for issue in issues:
        print(f"[STARTUP] {issue}")
    for fatal in fatal_issues:
        print(f"[STARTUP][FATAL] {fatal}")

    _STARTUP_RAN = True
    result = {
        "ok": ok,
        "issues": issues,
        "fatal_issues": fatal_issues,
        "gpu_layers": gpu_layers,
        "summary": summary,
    }
    if strict and not ok:
        raise SystemExit(1)
    return result


if __name__ == "__main__":
    run_startup_check(autofix=True, strict=True)
