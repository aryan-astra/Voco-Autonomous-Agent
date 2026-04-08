"""Typed data structures for VOCO agent session state."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A parsed tool invocation from the LLM response."""
    name: str
    args: dict[str, Any]


@dataclass
class HistoryEntry:
    """A single turn in the agent conversation."""
    role: str          # "user" | "assistant"
    content: str


@dataclass
class AgentContext:
    """Canonical session state passed through every agent component."""
    history: list[HistoryEntry] = field(default_factory=list)
    memory: str = ""                  # loaded from memory/project_state.md
    workspace: str = "./workspace"
    last_error: str | None = None
    step_count: int = 0
    tools: list[str] = field(default_factory=list)  # names of available tools
