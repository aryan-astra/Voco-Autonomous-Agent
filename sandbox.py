"""Workspace sandbox — enforces path jail and extension allowlist."""

from pathlib import Path

from constants import WORKSPACE_PATH, ALLOWED_EXTENSIONS


class SandboxViolation(Exception):
    """Raised when a tool attempts to access a path outside the sandbox."""


def safe_path(raw: str) -> Path:
    """
    Resolve a path and verify it is inside WORKSPACE_PATH.

    Parameters
    ----------
    raw : str
        Path string provided by the LLM (may be relative or absolute).

    Returns
    -------
    Path
        Absolute resolved path guaranteed to be inside the workspace.

    Raises
    ------
    SandboxViolation
        If the resolved path escapes the workspace or has a disallowed extension.
    """
    workspace = WORKSPACE_PATH.resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Treat all paths as relative to workspace unless already absolute inside it
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace / candidate

    resolved = candidate.resolve()

    # Path jail check
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise SandboxViolation(
            f"Path '{raw}' resolves outside workspace: {resolved}"
        )

    # Extension allowlist
    if resolved.suffix and resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise SandboxViolation(
            f"Extension '{resolved.suffix}' is not permitted. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    return resolved
