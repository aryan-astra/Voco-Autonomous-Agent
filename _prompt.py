"""Prompt builder and parser for VOCO execution-only JSON plans."""

import json
import os
import re

import yaml

from constants import (
    HISTORY_BUDGET,
    HISTORY_FILE,
    SYSTEM_PROMPT_BUDGET,
    USER_PROFILE_BUDGET,
    USER_PROFILE_FILE,
)
from context import AgentContext


def build_system_prompt(context: AgentContext) -> str:
    """
    Build the system prompt for VOCO execution-only mode.
    The model must output only a JSON array of action steps.
    """
    _ = context
    user_profile_text = _load_user_profile()
    history_text = _load_recent_history(max_events=10)
    tool_spec = _build_tool_spec()

    system_prompt = f"""You are VOCO, a Windows OS automation agent. You DO NOT chat. You DO NOT explain. You DO NOT ask questions.

YOUR ONLY OUTPUT FORMAT IS A JSON ARRAY OF STEPS. NOTHING ELSE.

Every response must be exactly a raw JSON array with no markdown and no extra text:
[
  {{"tool": "tool_name_here", "args": {{"arg1": "value1"}}, "reason": "one sentence why"}}
]

If you cannot complete the task with available tools, output:
[{{"tool": "report_failure", "args": {{"reason": "explain why in one sentence"}}, "reason": "task not executable"}}]

NEVER OUTPUT ANYTHING EXCEPT THE JSON ARRAY.

AVAILABLE TOOLS:
{tool_spec}

USER PROFILE:
{user_profile_text}

RECENT HISTORY (last 10 actions):
{history_text}

RULES:
1. Break the task into the minimum number of steps needed.
2. Each step must use exactly one tool from AVAILABLE TOOLS.
3. Tool names must match exactly. Do not invent tool names.
4. All argument values must be strings, numbers, or booleans.
5. Maximum 12 steps. If more are needed, use report_failure.
"""
    return system_prompt[:SYSTEM_PROMPT_BUDGET + USER_PROFILE_BUDGET + HISTORY_BUDGET + 4000]


def _load_user_profile() -> str:
    """Load and format the user profile from YAML vault."""
    if not os.path.exists(USER_PROFILE_FILE):
        return "No user profile loaded yet."
    try:
        with open(USER_PROFILE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as exc:
        return f"Profile load error: {exc}"

    if not content or content == "# User Profile":
        return "No user profile data yet."

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return f"Profile parse error: {exc}"

    if isinstance(data, dict):
        lines = [f"- {key}: {value}" for key, value in data.items()]
        return "\n".join(lines)[:USER_PROFILE_BUDGET]
    return content[:USER_PROFILE_BUDGET]


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
    return """Your last response was not valid JSON.

You must output ONLY a JSON array of steps. Example:
[{"tool": "open_browser", "args": {"url": "https://youtube.com"}, "reason": "open browser to YouTube"}]

Output the corrected JSON array now. Nothing else."""
