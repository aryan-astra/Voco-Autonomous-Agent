"""
VOCO Orchestrator - execution-only JSON-plan loop.
Flow: task -> prompt -> LLM plan -> parse -> dispatch tools -> summarize.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time

from constants import (
    CONTEXT_FILE,
    FORMAT_FAILURE_LOG,
    MAX_RETRIES,
    MAX_STEPS,
    OLLAMA_MODEL,
)
from context import AgentContext
from llm import (
    check_ollama_running,
    generate_conversation,
    generate,
    generate_with_history,
    get_last_model_used,
    get_last_num_ctx_used,
)
from memory import append_event
from tools import TOOL_REGISTRY, dispatch_tool
from _prompt import build_correction_prompt, build_system_prompt, parse_response


def run(task: str, context: AgentContext, ui_callback=None) -> str:
    """
    Execute a user task through VOCO's plan-and-dispatch loop.

    ui_callback signature: callback(message: str, level: str)
    levels: info | step | success | error
    """
    start_time = time.time()
    context.task = task
    context.steps = []
    context.tool_results = []
    model_used = "local-fastpath"

    def emit(message: str, level: str = "info") -> None:
        if ui_callback:
            ui_callback(message, level)

    format_failures = 0
    retries = 0
    raw_response = ""

    local_plan = _build_local_fastpath_plan(task)
    if local_plan is not None:
        plan = local_plan
        context.router_decision = "local_fastpath"
        context.router_confidence = 1.0
        emit("[VOCO] Using local fast-path for basic OS command.", "info")
    elif _is_conversational_prompt(task):
        context.router_decision = "llm_conversation"
        context.router_confidence = 0.95
        emit("[VOCO] Checking Ollama connection...", "info")
        if not check_ollama_running():
            error_msg = (
                "Conversation model is unavailable. "
                f"Run: ollama serve && ollama pull {OLLAMA_MODEL}"
            )
            emit(f"[VOCO] ERROR: {error_msg}", "error")
            _log_execution(
                task=task,
                success=False,
                steps_completed=0,
                format_failures=0,
                retries=0,
                final_output=error_msg,
                error=error_msg,
                elapsed_seconds=round(time.time() - start_time, 1),
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used="none",
            )
            return error_msg

        emit("[VOCO] Generating conversational response...", "info")
        conversation_ok, conversation_reply = generate_conversation(user_message=task)
        model_used = get_last_model_used()
        emit(f"[VOCO] Model selected: {model_used} (num_ctx={get_last_num_ctx_used()})", "info")
        elapsed = round(time.time() - start_time, 1)

        if conversation_ok:
            summary = f"[VOCO] OK {conversation_reply}"
            emit(summary, "success")
            _log_execution(
                task=task,
                success=True,
                steps_completed=0,
                format_failures=0,
                retries=0,
                final_output=conversation_reply,
                error=None,
                elapsed_seconds=elapsed,
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used=model_used,
            )
            return summary

        error_msg = f"Task cannot be completed: {conversation_reply}"
        emit(f"[VOCO] ERROR: {error_msg}", "error")
        _log_execution(
            task=task,
            success=False,
            steps_completed=0,
            format_failures=0,
            retries=0,
            final_output=error_msg,
            error=conversation_reply,
            elapsed_seconds=elapsed,
            router_decision=context.router_decision,
            router_confidence=context.router_confidence,
            model_used=model_used,
        )
        return error_msg
    else:
        context.router_decision = "llm_plan"
        context.router_confidence = 0.9
        emit("[VOCO] Checking Ollama connection...", "info")
        if not check_ollama_running():
            error_msg = (
                "Ollama is not running or no local model is available. "
                f"Run: ollama serve && ollama pull {OLLAMA_MODEL}"
            )
            emit(f"[VOCO] ERROR: {error_msg}", "error")
            _log_execution(
                task=task,
                success=False,
                steps_completed=0,
                format_failures=0,
                retries=0,
                final_output=error_msg,
                error=error_msg,
                elapsed_seconds=round(time.time() - start_time, 1),
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used="none",
            )
            return error_msg

        emit("[VOCO] Building context...", "info")
        system_prompt = build_system_prompt(context)

        emit("[VOCO] Generating action plan...", "info")
        raw_response = generate(system_prompt=system_prompt, user_message=task)
        model_used = get_last_model_used()
        emit(f"[VOCO] Model selected: {model_used} (num_ctx={get_last_num_ctx_used()})", "info")
        status, plan = parse_response(raw_response)

        if status == "format_failure":
            format_failures += 1
            emit("[VOCO] Format issue detected. Requesting correction...", "info")
            correction_messages = [
                {"role": "user", "content": task},
                {"role": "assistant", "content": raw_response},
                {"role": "user", "content": build_correction_prompt()},
            ]
            corrected_response = generate_with_history(system_prompt, correction_messages)
            retries += 1
            model_used = get_last_model_used()
            emit(f"[VOCO] Correction model: {model_used} (num_ctx={get_last_num_ctx_used()})", "info")
            status, plan = parse_response(corrected_response)
            _log_format_failure(
                task=task,
                raw_response=raw_response,
                corrected_response=corrected_response,
                correction_worked=(status == "ok"),
                model_used=model_used,
            )
            raw_response = corrected_response

        if status != "ok" or not plan:
            error_msg = "Failed to generate a valid action plan."
            emit(f"[VOCO] ERROR: {error_msg}", "error")
            _log_execution(
                task=task,
                success=False,
                steps_completed=0,
                format_failures=format_failures,
                retries=retries,
                final_output=raw_response[:300],
                error="plan_parse_failure",
                elapsed_seconds=round(time.time() - start_time, 1),
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used=model_used,
            )
            return error_msg

    max_steps_to_run = min(len(plan), MAX_STEPS)
    emit(f"[VOCO] Plan has {len(plan)} steps. Executing up to {max_steps_to_run}.", "info")

    steps_completed = 0
    execution_failed = False
    final_output = ""

    for index, step in enumerate(plan[:MAX_STEPS], start=1):
        if not isinstance(step, dict):
            result = {"status": "error", "result": None, "message": "Invalid step payload (expected object)."}
            tool_name = "unknown"
            tool_args = {}
            reason = "invalid-step"
        else:
            tool_name = str(step.get("tool", "")).strip()
            tool_args = step.get("args", {})
            reason = str(step.get("reason", "")).strip()
            if not isinstance(tool_args, dict):
                result = {"status": "error", "result": None, "message": "Step args must be an object."}
            elif tool_name not in TOOL_REGISTRY:
                result = {"status": "error", "result": None, "message": f"Unknown tool: '{tool_name}'"}
            else:
                result = dispatch_tool(tool_name, tool_args)

        context.steps.append(
            {
                "step": index,
                "tool": tool_name,
                "args": tool_args,
                "reason": reason,
            }
        )
        context.tool_results.append(
            {
                "step": index,
                "tool": tool_name,
                "args": tool_args,
                "result": result,
            }
        )

        emit(
            f"[VOCO] Step {index}/{max_steps_to_run}: {tool_name}({_format_args_preview(tool_args)}) - {reason}",
            "step",
        )

        if result["status"] == "success":
            steps_completed += 1
            final_output = result["message"]
            emit(f"  OK {result['message']}", "success")
            continue

        if result["status"] == "failure":
            final_output = result["message"]
            emit(f"  FAIL {result['message']}", "error")
            execution_failed = True
            break

        final_output = result["message"]
        emit(f"  ERR {result['message']}", "error")
        if index == max_steps_to_run:
            execution_failed = True

    if len(plan) > MAX_STEPS:
        emit(f"[VOCO] Step limit ({MAX_STEPS}) reached. Task may be incomplete.", "info")
        _write_incomplete_state(task=task, plan=plan, tool_results=context.tool_results)

    success = steps_completed > 0 and not execution_failed
    elapsed = round(time.time() - start_time, 1)

    _log_execution(
        task=task,
        success=success,
        steps_completed=steps_completed,
        format_failures=format_failures,
        retries=retries,
        final_output=final_output,
        error=None if success else "execution_error",
        elapsed_seconds=elapsed,
        router_decision=context.router_decision,
        router_confidence=context.router_confidence,
        model_used=model_used,
    )

    icon = "OK" if success else "FAIL"
    summary = (
        f"[VOCO] {icon} Task complete in {elapsed}s. "
        f"Steps: {steps_completed}/{max_steps_to_run}. Result: {final_output}"
    )
    emit(summary, "success" if success else "error")
    return summary


def _format_args_preview(args: dict, max_length: int = 60) -> str:
    if not args:
        return ""
    parts = [f"{key}={repr(value)[:20]}" for key, value in args.items()]
    preview = ", ".join(parts)
    if len(preview) > max_length:
        return preview[:max_length] + "..."
    return preview


def _log_execution(
    task: str,
    success: bool,
    steps_completed: int,
    format_failures: int,
    retries: int,
    final_output: str,
    error: str | None = None,
    elapsed_seconds: float = 0.0,
    router_decision: str = "unknown",
    router_confidence: float = 0.0,
    model_used: str = OLLAMA_MODEL,
) -> None:
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task": task,
        "success": success,
        "steps_completed": steps_completed,
        "format_failures": format_failures,
        "retries": retries,
        "final_output": final_output[:300] if final_output else "",
        "error": error,
        "elapsed_seconds": elapsed_seconds,
        "router_decision": router_decision,
        "router_confidence": router_confidence,
        "model": model_used,
    }
    append_event(record)


def _log_format_failure(
    task: str,
    raw_response: str,
    corrected_response: str,
    correction_worked: bool,
    model_used: str = OLLAMA_MODEL,
) -> None:
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task": task,
        "raw_response": raw_response[:500],
        "corrected_response": corrected_response[:500],
        "correction_worked": correction_worked,
        "model": model_used,
    }
    failure_path = os.path.abspath(FORMAT_FAILURE_LOG)
    os.makedirs(os.path.dirname(failure_path), exist_ok=True)
    with open(failure_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_incomplete_state(task: str, plan: list, tool_results: list) -> None:
    incomplete_path = os.path.abspath(CONTEXT_FILE)
    os.makedirs(os.path.dirname(incomplete_path), exist_ok=True)
    last_message = ""
    if tool_results:
        last_result = tool_results[-1].get("result", {})
        if isinstance(last_result, dict):
            last_message = str(last_result.get("message", ""))
    with open(incomplete_path, "a", encoding="utf-8") as f:
        f.write(f"\n## Incomplete Task - {datetime.datetime.now().isoformat()}\n")
        f.write(f"**Task:** {task}\n")
        f.write(f"**Completed:** {len(tool_results)} steps\n")
        f.write(f"**Last output:** {last_message}\n")
        f.write(f"**Remaining steps:** {max(len(plan) - len(tool_results), 0)}\n\n")


def _build_local_fastpath_plan(task: str) -> list[dict] | None:
    """Return a direct tool plan for simple commands that do not require LLM planning."""
    text = task.lower().strip()

    unmute_patterns = [
        r"\bun[-\s]?mute\b",
        r"\bur[-\s]?mute\b",
        r"\bturn on (?:the )?(?:system )?audio\b",
        r"\baudio on\b",
        r"\bsound on\b",
    ]
    if any(re.search(pattern, text) for pattern in unmute_patterns):
        return [
            {
                "tool": "mute_audio",
                "args": {"mute": False},
                "reason": "Direct local command for unmuting system audio.",
            }
        ]

    if re.search(r"\bmute\b", text):
        return [
            {
                "tool": "mute_audio",
                "args": {"mute": True},
                "reason": "Direct local command for muting system audio.",
            }
        ]

    if "take a screenshot" in text or "take screenshot" in text or "capture screenshot" in text:
        return [
            {
                "tool": "take_screenshot",
                "args": {},
                "reason": "Direct local command for screenshot capture.",
            }
        ]

    if "running apps" in text or "what apps are currently running" in text:
        return [
            {
                "tool": "get_running_apps",
                "args": {},
                "reason": "Direct local command for listing active windows.",
            }
        ]

    app_name = _extract_core_app_name(text)
    if app_name is not None:
        return [
            {
                "tool": "open_app",
                "args": {"app_name": app_name},
                "reason": "Direct local command for opening a core demo application.",
            }
        ]

    browser_plan = _build_browser_fastpath_plan(task=task, text=text)
    if browser_plan is not None:
        return browser_plan

    file_plan = _build_file_generation_fastpath_plan(task=task, text=text)
    if file_plan is not None:
        return file_plan

    return None


def _extract_core_app_name(text: str) -> str | None:
    app_patterns = [
        (r"\bopen\s+notepad\b", "notepad"),
        (r"\bopen\s+calculator\b|\bopen\s+calc\b", "calculator"),
        (r"\bopen\s+file\s+explorer\b|\bopen\s+explorer\b", "explorer"),
        (r"\bopen\s+settings\b", "settings"),
    ]
    for pattern, app_name in app_patterns:
        if re.search(pattern, text):
            return app_name
    return None


def _build_browser_fastpath_plan(task: str, text: str) -> list[dict] | None:
    browser_tokens = [
        "open browser",
        "open chrome",
        "open edge",
        "open firefox",
        "open google",
        "open youtube",
        "go to ",
    ]
    if not any(token in text for token in browser_tokens):
        return None

    url = _extract_browser_url(task=task, text=text)
    if url is None:
        url = "https://www.google.com"

    return [
        {
            "tool": "run_shell_command",
            "args": {"command": f'Start-Process "{url}"'},
            "reason": "Direct local command to open a URL in the default browser.",
        }
    ]


def _extract_browser_url(task: str, text: str) -> str | None:
    explicit_match = re.search(r"(https?://[^\s\"']+)", task, flags=re.IGNORECASE)
    if explicit_match:
        return explicit_match.group(1)

    go_to_match = re.search(
        r"\bgo to\s+([a-z0-9\-\.]+\.[a-z]{2,}(?:/[^\s]*)?)",
        text,
        flags=re.IGNORECASE,
    )
    if go_to_match:
        return f"https://{go_to_match.group(1)}"

    if "youtube" in text:
        return "https://www.youtube.com"
    if "google" in text:
        return "https://www.google.com"
    return None


def _build_file_generation_fastpath_plan(task: str, text: str) -> list[dict] | None:
    write_tokens = ["write", "create", "generate", "make"]
    if not any(token in text for token in write_tokens):
        return None
    if ".py" not in text and "python file" not in text and "python script" not in text:
        return None

    filename = _extract_python_filename(task=task, text=text)
    if filename is None:
        filename = "generated_script.py"
    content = _build_python_template_content(text=text)

    return [
        {
            "tool": "write_file",
            "args": {"path": filename, "content": content},
            "reason": "Direct local command to create a demo Python file in workspace.",
        }
    ]


def _extract_python_filename(task: str, text: str) -> str | None:
    file_match = re.search(r"([a-zA-Z0-9_\-]+\.py)\b", task)
    if file_match:
        return file_match.group(1)

    named_match = re.search(r"\bnamed\s+([a-z0-9_\-]+)\b", text)
    if named_match:
        return f"{named_match.group(1)}.py"

    if "odd" in text and "even" in text:
        return "odd_even_checker.py"
    return None


def _build_python_template_content(text: str) -> str:
    if "odd" in text and "even" in text:
        return (
            "def is_even(number: int) -> bool:\n"
            "    return number % 2 == 0\n\n"
            "def main() -> None:\n"
            "    raw_value = input(\"Enter an integer: \").strip()\n"
            "    try:\n"
            "        number = int(raw_value)\n"
            "    except ValueError:\n"
            "        print(\"Please enter a valid integer.\")\n"
            "        return\n\n"
            "    if is_even(number):\n"
            "        print(f\"{number} is even.\")\n"
            "    else:\n"
            "        print(f\"{number} is odd.\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    return (
        "def main() -> None:\n"
        "    print(\"TODO: implement this script\")\n\n"
        "if __name__ == \"__main__\":\n"
        "    main()\n"
    )


def _is_conversational_prompt(task: str) -> bool:
    """Return True when the input should be handled as conversational chat."""
    text = task.lower().strip()
    if not text:
        return True

    conversational_patterns = [
        r"^(hi+|hello|hey+)(\s+voco)?[!.?]*$",
        r"^(thanks|thank you|thx)[!.?]*$",
        r"^what(?:'s| is)\s+your\s+name[?.!]*$",
        r"^who\s+are\s+you[?.!]*$",
        r"^what\s+can\s+you\s+do[?.!]*$",
        r"^how\s+are\s+you[?.!]*$",
        r"^tell me about yourself[?.!]*$",
    ]
    return any(re.match(pattern, text) for pattern in conversational_patterns)
