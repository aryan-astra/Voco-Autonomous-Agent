"""Shared VOCO agent execution context."""

from dataclasses import dataclass, field

from constants import WORKSPACE_PATH


@dataclass
class AgentContext:
    """Mutable context shared through the VOCO execution loop."""

    task: str = ""
    steps: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    workspace: str = str(WORKSPACE_PATH)
    router_decision: str = "unknown"
    router_confidence: float = 0.0
    decomposition_used: bool = False
    decomposed_steps: list[str] = field(default_factory=list)
    step_routes: list[dict] = field(default_factory=list)
    access_level_policy: str = "L1-L3"


def create_context() -> AgentContext:
    """Return a fresh context."""
    return AgentContext()


def reset_context(ctx: AgentContext) -> AgentContext:
    """Reset volatile task state while preserving persistent fields."""
    ctx.task = ""
    ctx.steps.clear()
    ctx.tool_results.clear()
    ctx.history.clear()
    ctx.router_decision = "unknown"
    ctx.router_confidence = 0.0
    ctx.decomposition_used = False
    ctx.decomposed_steps.clear()
    ctx.step_routes.clear()
    return ctx
