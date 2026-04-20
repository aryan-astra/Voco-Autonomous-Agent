"""Prompt builder and parser for VOCO execution-only JSON plans."""

import json
import os
import re

from constants import (
    DEMO_INCLUDE_HISTORY_CONTEXT,
    DEMO_INCLUDE_PROFILE_CONTEXT,
    HISTORY_BUDGET,
    HISTORY_FILE,
    SYSTEM_PROMPT_BUDGET,
    USER_PROFILE_BUDGET,
)
from context import AgentContext
from memory import SecureMemoryError, load_user_profile_dict
from skills.skills_registry import find_matching_skill, format_skill_hint


def build_system_prompt(context: AgentContext) -> str:
    """
    Build the system prompt for VOCO execution-only mode.
    The model must output only a JSON array of action steps.
    """
    _ = context
    tool_spec = _build_tool_spec()
    profile_block = ""
    history_block = ""
    if DEMO_INCLUDE_PROFILE_CONTEXT:
        user_profile_text = _load_user_profile()
        profile_block = f"""
USER PROFILE:
{user_profile_text}
"""
    if DEMO_INCLUDE_HISTORY_CONTEXT:
        history_text = _load_recent_history(max_events=10)
        history_block = f"""
RECENT HISTORY (last 10 actions):
{history_text}
"""
    task_text = str(getattr(context, "task", "")).strip()
    skill = find_matching_skill(task_text)
    skill_block = f"\n{format_skill_hint(skill)}\n" if skill is not None else ""

    system_prompt = f"""You are VOCO, a Windows OS automation agent. You DO NOT chat. You DO NOT explain. You DO NOT ask questions.

OUTPUT CONTRACT (MANDATORY):
- Output exactly one raw JSON array and nothing else.
- The response must be deterministic, valid JSON (no markdown, no code fences, no comments, no trailing commas).
- Each array item must be an object with keys: "tool", "args", "reason".
- "tool" must be one exact tool name from AVAILABLE TOOLS.
- "args" must be a JSON object.
- "reason" must be one short sentence.

Example:
[
  {{"tool": "browser_navigate", "args": {{"url": "https://example.com"}}, "reason": "open requested page"}}
]

If you cannot complete the task with available tools, output:
[{{"tool": "report_failure", "args": {{"reason": "explain why in one sentence"}}, "reason": "task not executable"}}]

NEVER OUTPUT ANYTHING EXCEPT THE JSON ARRAY.

AVAILABLE TOOLS:
{tool_spec}
{skill_block}
{profile_block}{history_block}

RULES:
1. Plan deterministically; avoid optional branching or duplicate alternatives.
2. Each step must call exactly one tool from AVAILABLE TOOLS.
3. Read state before acting when possible: use browser_get_state after browser actions and get_window_state around desktop interactions.
4. For browser tasks, follow a closed-loop cycle: act with browser_* tool, then read browser_get_state before deciding the next action.
5. Browser input submission policy:
   - Use Enter only when the user explicitly intends submit/send/search.
   - Use Shift+Enter for multiline/newline in compose/chat text boxes.
6. Avoid duplicate open/focus actions unless user explicitly requested reopen/refocus.
7. Tool names must match exactly. Do not invent tool names.
8. All argument values must be strings, numbers, or booleans.
9. Keep plans concise and deterministic. Maximum 12 steps.
10. If task needs privileged system change, set args.human_approval=true only when user explicitly approved it.
11. If task cannot be completed with tools, use report_failure.
"""
    return system_prompt[:SYSTEM_PROMPT_BUDGET + USER_PROFILE_BUDGET + HISTORY_BUDGET + 4000]


def _load_user_profile() -> str:
    """Load and format the user profile from secure vault storage."""
    try:
        data = load_user_profile_dict()
    except SecureMemoryError as exc:
        return f"Profile load error: {exc}"

    if not data:
        return "No user profile data yet."

    lines = [f"- {key}: {value}" for key, value in data.items()]
    return "\n".join(lines)[:USER_PROFILE_BUDGET]


def _load_recent_history(max_events: int = 10) -> str:
    """Load recent execution history from JSONL vault."""
    if not os.path.exists(HISTORY_FILE):
        return "No history yet."
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        return f"History load error: {exc}"

    recent = lines[-max_events:] if len(lines) > max_events else lines
    events: list[str] = []
    for line in recent:
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        timestamp = str(event.get("timestamp", ""))[:16]
        task = str(event.get("task", ""))
        success_marker = "OK" if event.get("success") else "FAIL"
        events.append(f"{timestamp} {success_marker} {task}")

    history = "\n".join(events) if events else "No history yet."
    return history[:HISTORY_BUDGET]


def _build_tool_spec() -> str:
    """Build the tool specification string from the tool registry."""
    try:
        from tools import TOOL_REGISTRY
    except Exception as exc:  # pragma: no cover - defensive import path
        return f"Tool registry error: {exc}"

    spec_lines: list[str] = []
    for name, info in TOOL_REGISTRY.items():
        description = info.get("description", "")
        args = info.get("args", {})
        args_str = ", ".join([f"{k}: {v}" for k, v in args.items()])
        spec_lines.append(f"- {name}({args_str}): {description}")
    return "\n".join(spec_lines)


def parse_response(response: str) -> tuple[str, list | None]:
    """
    Parse a model response and extract a JSON action plan.

    Returns (status, plan):
      - ("ok", list)
      - ("format_failure", None)
      - ("empty", None)
    """
    response = response.strip()
    if not response:
        return "empty", None

    try:
        plan = json.loads(response)
        if isinstance(plan, list) and len(plan) > 0:
            return "ok", plan
    except json.JSONDecodeError:
        pass

    code_block_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL)
    if code_block_match:
        try:
            plan = json.loads(code_block_match.group(1))
            if isinstance(plan, list) and len(plan) > 0:
                return "ok", plan
        except json.JSONDecodeError:
            pass

    first_bracket = response.find("[")
    last_bracket = response.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            plan = json.loads(response[first_bracket : last_bracket + 1])
            if isinstance(plan, list) and len(plan) > 0:
                return "ok", plan
        except json.JSONDecodeError:
            pass

    return "format_failure", None


def build_correction_prompt() -> str:
    """Return the follow-up prompt used after a format failure."""
    return """Your last response was not valid JSON for VOCO execution mode.

Respond with ONLY a valid JSON array of step objects in this exact shape:
[{"tool":"browser_navigate","args":{"url":"https://example.com"},"reason":"open requested page"}]

Rules:
- Use only tool names from AVAILABLE TOOLS.
- Keep args as a JSON object.
- Do not add markdown, code fences, or extra text.

Output the corrected JSON array now."""
