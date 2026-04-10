"""
VOCO Orchestrator - execution-only JSON-plan loop.
Flow: task -> prompt -> LLM plan -> parse -> dispatch tools -> summarize.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import re
import time
from pathlib import Path

from constants import (
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
from memory import SecureMemoryError, append_context_entry, append_event, append_memory
from router import predict_route
from tools import TOOL_REGISTRY, dispatch_tool, tool_requires_approval
from _prompt import build_correction_prompt, build_system_prompt, parse_response


_MINIMAL_CONTEXT_MAX_CHARS = 2200
_DEFAULT_TOOL_RESULT_SUMMARY_TOKENS = 120

_DECOMPOSER_MODULE_PATH = Path(__file__).with_name("tools").joinpath("decomposer.py")
_decomposer_spec = importlib.util.spec_from_file_location("task_decomposer", _DECOMPOSER_MODULE_PATH)
if _decomposer_spec is None or _decomposer_spec.loader is None:
    raise ImportError(f"Unable to load decomposer module from '{_DECOMPOSER_MODULE_PATH}'.")
_task_decomposer = importlib.util.module_from_spec(_decomposer_spec)
_decomposer_spec.loader.exec_module(_task_decomposer)
needs_decomposition = _task_decomposer.needs_decomposition
decompose_task = _task_decomposer.decompose_task

_USER_PROFILE_MODULE_PATH = Path(__file__).with_name("memory").joinpath("user_profile.py")
_FS_WATCHER_MODULE_PATH = Path(__file__).with_name("memory").joinpath("fs_watcher.py")
_user_profile_class: type | None = None
_user_profile_class_attempted = False
_user_profile_store: object | None = None
_user_profile_store_attempted = False
_fs_watcher_module: object | None = None
_fs_watcher_module_attempted = False
_fs_watcher_observer: object | None = None


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 8:
        return text[:max_chars]
    suffix = f"...(+{len(text) - max_chars})"
    if len(suffix) >= max_chars:
        return text[: max_chars - 3] + "..."
    cutoff = max_chars - len(suffix)
    overflow = len(text) - cutoff
    suffix = f"...(+{overflow})"
    if len(suffix) >= max_chars:
        return text[: max_chars - 3] + "..."
    return text[: max_chars - len(suffix)] + suffix


def _safe_json_dumps(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _estimate_text_size(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    return len(_safe_json_dumps(value))


def _compact_value(value: object, char_budget: int, depth: int = 0) -> object:
    budget = max(16, char_budget)

    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _truncate_text(_normalize_whitespace(value), budget)
    if depth >= 2:
        return _truncate_text(_normalize_whitespace(_safe_json_dumps(value)), budget)

    if isinstance(value, dict):
        max_items = 6 if depth == 0 else 4
        per_item_budget = max(36, budget // max(1, max_items))
        compacted: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                break
            compacted[str(key)] = _compact_value(item, per_item_budget, depth + 1)
        omitted = max(0, len(value) - len(compacted))
        if omitted:
            compacted["_omitted_keys"] = omitted
        return compacted

    if isinstance(value, (list, tuple)):
        max_items = 4 if depth == 0 else 3
        per_item_budget = max(28, budget // max(1, max_items))
        compacted_list = [_compact_value(item, per_item_budget, depth + 1) for item in value[:max_items]]
        omitted = max(0, len(value) - len(compacted_list))
        if omitted:
            compacted_list.append(f"... {omitted} more item(s)")
        return compacted_list

    return _truncate_text(_normalize_whitespace(str(value)), budget)


def summarize_tool_result(result: object, max_tokens: int = _DEFAULT_TOOL_RESULT_SUMMARY_TOKENS) -> dict:
    token_budget = _coerce_positive_int(max_tokens, _DEFAULT_TOOL_RESULT_SUMMARY_TOKENS)
    char_budget = min(8000, max(200, token_budget * 4))

    if not isinstance(result, dict):
        payload_summary = _compact_value(result, max(80, char_budget - 48))
        return {
            "status": "unknown",
            "message": "Tool returned non-dict payload.",
            "result": payload_summary,
            "debug": {
                "raw_type": type(result).__name__,
                "approx_chars": _estimate_text_size(result),
                "summary_chars": _estimate_text_size(payload_summary),
                "truncated": True,
            },
        }

    status = _normalize_whitespace(str(result.get("status", ""))).lower() or "unknown"
    message_budget = max(80, min(260, char_budget // 3))
    message = _truncate_text(_normalize_whitespace(str(result.get("message", ""))), message_budget)
    raw_payload = result.get("result")
    payload_budget = max(90, char_budget - len(message) - 32)
    payload_summary = _compact_value(raw_payload, payload_budget)
    raw_chars = _estimate_text_size(raw_payload)
    summary_chars = _estimate_text_size(payload_summary)

    return {
        "status": status,
        "message": message,
        "result": payload_summary,
        "debug": {
            "raw_type": type(raw_payload).__name__,
            "approx_chars": raw_chars,
            "summary_chars": summary_chars,
            "truncated": summary_chars < raw_chars,
        },
    }


def _build_memory_summary(context: AgentContext, max_tokens: int = 80) -> str:
    token_budget = _coerce_positive_int(max_tokens, 80)
    char_budget = min(2400, max(180, token_budget * 4))
    segments: list[str] = []

    if isinstance(context.memory, dict) and context.memory:
        memory_snapshot = _compact_value(context.memory, char_budget // 2)
        segments.append(f"memory={_safe_json_dumps(memory_snapshot)}")

    if isinstance(context.history, list) and context.history:
        history_snapshot = _compact_value(context.history[-3:], char_budget // 2)
        segments.append(f"history={_safe_json_dumps(history_snapshot)}")

    if isinstance(context.tool_results, list) and context.tool_results:
        last_item = context.tool_results[-1]
        if isinstance(last_item, dict):
            last_payload = last_item.get("result")
            if last_payload is not None:
                last_summary = summarize_tool_result(last_payload, max_tokens=40)
                segments.append(f"last_tool={_safe_json_dumps(last_summary)}")

    if not segments:
        return "none"
    return _truncate_text(_normalize_whitespace(" | ".join(segments)), char_budget)


def build_minimal_context(task: str, step: str | None, last_result: object, memory_summary: str) -> str:
    task_text = _truncate_text(_normalize_whitespace(task), 700)
    step_text = _truncate_text(_normalize_whitespace(step or ""), 340)
    memory_text = _truncate_text(_normalize_whitespace(memory_summary), 540)

    parts = [f"TASK: {task_text}"]
    if step_text:
        parts.append(f"CURRENT_STEP: {step_text}")
        parts.append("FOCUS: Resolve CURRENT_STEP while staying consistent with TASK.")
    if last_result is not None:
        last_summary = summarize_tool_result(last_result, max_tokens=80)
        parts.append(f"LAST_RESULT: {_truncate_text(_safe_json_dumps(last_summary), 900)}")
    if memory_text and memory_text != "none":
        parts.append(f"MEMORY: {memory_text}")
    parts.append("OUTPUT: Return only a JSON array of tool steps.")
    return _truncate_text("\n".join(parts), _MINIMAL_CONTEXT_MAX_CHARS)


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
    self_heal_retries = 0
    self_heal_outcomes: list[dict] = []
    raw_response = ""
    _ensure_fs_watcher_started()
    _record_user_profile_task(task=task)

    tool_first_bundle = _build_tool_first_hybrid_plan(task=task, context=context, emit=emit)
    local_plan = _build_local_fastpath_plan(task) if tool_first_bundle is None else None

    if tool_first_bundle is not None:
        bundle_error = str(tool_first_bundle.get("error", "")).strip()
        context.router_decision = "tool_first_hybrid"
        context.router_confidence = float(tool_first_bundle.get("confidence", 0.9))
        context.decomposition_used = bool(tool_first_bundle.get("decomposition_used"))
        context.decomposed_steps = list(tool_first_bundle.get("split_steps", []))
        context.step_routes = list(tool_first_bundle.get("route_trace", []))
        format_failures += int(tool_first_bundle.get("format_failures", 0))
        retries += int(tool_first_bundle.get("plan_retries", 0))

        split_steps = context.decomposed_steps
        if split_steps:
            emit(f"[VOCO] Task splitter produced {len(split_steps)} atomic steps.", "info")

        bundle_model = str(tool_first_bundle.get("model_used", "")).strip()
        if bundle_model:
            model_used = bundle_model
            emit(f"[VOCO] Model selected: {model_used} (num_ctx={get_last_num_ctx_used()})", "info")

        if bundle_error:
            emit(f"[VOCO] ERROR: {bundle_error}", "error")
            _log_execution(
                task=task,
                success=False,
                steps_completed=0,
                format_failures=format_failures,
                retries=retries,
                final_output=bundle_error,
                error=bundle_error,
                elapsed_seconds=round(time.time() - start_time, 1),
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used=model_used,
                steps=context.steps,
                tool_results=context.tool_results,
                decomposition_used=context.decomposition_used,
                decomposed_steps=context.decomposed_steps,
                step_routes=context.step_routes,
                access_level_policy=context.access_level_policy,
            )
            return bundle_error

        plan = tool_first_bundle.get("plan", [])
        if not isinstance(plan, list) or not plan:
            error_msg = "Tool-first planner produced no executable steps."
            emit(f"[VOCO] ERROR: {error_msg}", "error")
            _log_execution(
                task=task,
                success=False,
                steps_completed=0,
                format_failures=format_failures,
                retries=retries,
                final_output=error_msg,
                error=error_msg,
                elapsed_seconds=round(time.time() - start_time, 1),
                router_decision=context.router_decision,
                router_confidence=context.router_confidence,
                model_used=model_used,
                steps=context.steps,
                tool_results=context.tool_results,
                decomposition_used=context.decomposition_used,
                decomposed_steps=context.decomposed_steps,
                step_routes=context.step_routes,
                access_level_policy=context.access_level_policy,
            )
            return error_msg

        emit("[VOCO] Using tool-first decomposition and per-step routing.", "info")
    elif local_plan is not None:
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
                steps=context.steps,
                tool_results=context.tool_results,
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
                steps=context.steps,
                tool_results=context.tool_results,
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
            steps=context.steps,
            tool_results=context.tool_results,
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
                steps=context.steps,
                tool_results=context.tool_results,
            )
            return error_msg

        emit("[VOCO] Building context...", "info")
        system_prompt = build_system_prompt(context)
        memory_summary = _build_memory_summary(context=context, max_tokens=90)
        planning_context = build_minimal_context(
            task=task,
            step="Generate a deterministic execution plan for the full task.",
            last_result=context.tool_results[-1].get("result") if context.tool_results else None,
            memory_summary=memory_summary,
        )

        emit("[VOCO] Generating action plan...", "info")
        raw_response = generate(system_prompt=system_prompt, user_message=planning_context)
        model_used = get_last_model_used()
        emit(f"[VOCO] Model selected: {model_used} (num_ctx={get_last_num_ctx_used()})", "info")
        status, plan = parse_response(raw_response)

        if status == "format_failure":
            format_failures += 1
            emit("[VOCO] Format issue detected. Requesting correction...", "info")
            correction_signal = {
                "status": "format_failure",
                "message": "Planner output was not valid JSON.",
                "result": raw_response,
            }
            correction_context = build_minimal_context(
                task=task,
                step="Correct the planner output into one valid JSON array.",
                last_result=correction_signal,
                memory_summary=memory_summary,
            )
            correction_messages = [
                {"role": "user", "content": correction_context},
                {"role": "assistant", "content": _safe_json_dumps(summarize_tool_result(correction_signal, 90))},
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
                steps=context.steps,
                tool_results=context.tool_results,
            )
            return error_msg

    max_steps_to_run = min(len(plan), MAX_STEPS)
    max_step_retries = max(0, int(MAX_RETRIES))
    emit(f"[VOCO] Plan has {len(plan)} steps. Executing up to {max_steps_to_run}.", "info")

    steps_completed = 0
    execution_failed = False
    final_output = ""

    for index, step in enumerate(plan[:MAX_STEPS], start=1):
        requires_approval = False
        human_approved = False
        privileged_action = False
        approval_error_code = ""
        tool_name = "unknown"
        tool_args: dict = {}
        reason = ""
        step_reason = ""
        policy_metadata = _build_policy_metadata(
            tool_name=tool_name,
            requires_approval=requires_approval,
            human_approved=human_approved,
            policy_scope=context.access_level_policy,
            approval_error_code=approval_error_code,
        )
        if not isinstance(step, dict):
            result = {"status": "error", "result": None, "message": "Invalid step payload (expected object)."}
            reason = "invalid-step"
        else:
            tool_name = str(step.get("tool", "")).strip()
            tool_args = step.get("args", {})
            reason = str(step.get("reason", "")).strip()
            if tool_name in TOOL_REGISTRY and isinstance(tool_args, dict):
                requires_approval = tool_requires_approval(tool_name)
                privileged_action = _is_privileged_tool_action(
                    tool_name=tool_name,
                    requires_approval=requires_approval,
                )
                if privileged_action:
                    human_approved = _has_human_approval(task=task, args=tool_args)

            if not isinstance(tool_args, dict):
                result = {"status": "error", "result": None, "message": "Step args must be an object."}
            elif tool_name not in TOOL_REGISTRY:
                result = {"status": "error", "result": None, "message": f"Unknown tool: '{tool_name}'"}
            elif privileged_action and not human_approved:
                approval_error_code = _POLICY_APPROVAL_ERROR_CODE
                policy_metadata = _build_policy_metadata(
                    tool_name=tool_name,
                    requires_approval=requires_approval,
                    human_approved=human_approved,
                    policy_scope=context.access_level_policy,
                    approval_error_code=approval_error_code,
                )
                result = _build_approval_required_result(tool_name=tool_name, policy_metadata=policy_metadata)
            else:
                result = dispatch_tool(tool_name, tool_args)

        policy_metadata = _build_policy_metadata(
            tool_name=tool_name,
            requires_approval=requires_approval,
            human_approved=human_approved,
            policy_scope=context.access_level_policy,
            approval_error_code=approval_error_code,
        )
        step_reason = _format_step_reason_with_policy(reason=reason, policy_metadata=policy_metadata)
        emit(
            (
                f"[VOCO] Step {index}/{max_steps_to_run}: "
                f"{tool_name}({_format_args_preview(tool_name, tool_args)}) - {step_reason}"
            ),
            "step",
        )

        retry_attempts: list[dict] = []
        retry_count = 0
        retries_exhausted = False
        trigger_failure_class = ""
        trigger_known_fix = ""
        last_failure_class = ""
        last_known_fix = ""
        last_error_message = ""
        last_correction_context: dict[str, object] = {}

        for attempt_number in range(1, max_step_retries + 2):
            if attempt_number > 1:
                result = dispatch_tool(tool_name, tool_args)

            classification = _classify_tool_failure(tool_name=tool_name, result=result)
            status = str(result.get("status", "")).strip().lower()
            message = str(result.get("message", "")).strip()
            correction_context: dict[str, object] = {}
            known_fix_hint = str(classification["known_fix"])
            if status != "success":
                correction_context = _build_retry_correction_context(
                    tool_name=tool_name,
                    failure_class=str(classification["class"]),
                    known_fix=str(classification["known_fix"]),
                    error_message=message,
                )
                selected_hint = _normalize_whitespace(str(correction_context.get("selected_hint", "")))
                if selected_hint:
                    known_fix_hint = selected_hint
                if not trigger_failure_class:
                    trigger_failure_class = str(classification["class"])
                    trigger_known_fix = known_fix_hint
                last_failure_class = str(classification["class"])
                last_known_fix = known_fix_hint
                last_error_message = message[:300]
                last_correction_context = correction_context

            remaining_retry_budget = max_step_retries - (attempt_number - 1)
            should_retry = status != "success" and bool(classification["recoverable"]) and remaining_retry_budget > 0

            retry_attempts.append(
                {
                    "attempt": attempt_number,
                    "status": status or "unknown",
                    "message": message[:300],
                    "classification": str(classification["class"]),
                    "recoverable": bool(classification["recoverable"]),
                    "known_fix": known_fix_hint,
                    "hint_source": str(correction_context.get("selected_hint_source", "builtin")).strip() or "builtin",
                    "profile_hint_count": max(0, _safe_int(correction_context.get("profile_match_count"), 0)),
                    "will_retry": should_retry,
                }
            )

            if should_retry:
                retry_count += 1
                self_heal_retries += 1
                emit(
                    (
                        f"  RETRY {retry_count}/{max_step_retries} due to "
                        f"{classification['class']}: {known_fix_hint}"
                    ),
                    "info",
                )
                continue

            retries_exhausted = status != "success" and bool(classification["recoverable"]) and remaining_retry_budget == 0
            break

        retry_metadata = {
            "max_retries": max_step_retries,
            "retry_count": retry_count,
            "retries_exhausted": retries_exhausted,
            "trigger_failure_class": trigger_failure_class,
            "known_fix": trigger_known_fix or last_known_fix,
            "attempts": retry_attempts,
        }
        if last_correction_context:
            retry_metadata["correction_context"] = last_correction_context

        if retries_exhausted:
            failure_class = last_failure_class or trigger_failure_class or "unknown"
            exhausted_message = (
                f"Retries exhausted for step {index} ({tool_name}). "
                f"failure_class={failure_class}; retries={retry_count}/{max_step_retries}. "
                f"Last error: {last_error_message}"
            )
            learn_teach_affordance = _build_learn_teach_affordance(
                tool_name=tool_name,
                failure_class=failure_class,
                retry_count=retry_count,
                max_retries=max_step_retries,
                correction_context=last_correction_context,
            )
            retry_metadata["learn_teach_affordance"] = learn_teach_affordance
            result = {
                "status": "failure",
                "result": result.get("result") if isinstance(result, dict) else None,
                "message": exhausted_message,
                "learn_teach_affordance": learn_teach_affordance,
            }
            retry_metadata["exhausted_message"] = exhausted_message

        if retry_count > 0:
            self_heal_outcomes.append(
                {
                    "step": index,
                    "tool": tool_name,
                    "trigger_failure_class": trigger_failure_class or "unknown",
                    "known_fix": trigger_known_fix or last_known_fix,
                    "retry_count": retry_count,
                    "resolved": str(result.get("status", "")).strip().lower() == "success",
                    "retries_exhausted": retries_exhausted,
                    "last_error": last_error_message,
                }
            )

        safe_tool_args = _sanitize_tool_args(tool_name=tool_name, args=tool_args)
        context.steps.append(
            {
                "step": index,
                "tool": tool_name,
                "args": safe_tool_args,
                "reason": step_reason,
                "requires_approval": requires_approval,
                "human_approved": human_approved,
                "access_level": policy_metadata["access_level"],
                "policy": policy_metadata,
                "retry": retry_metadata,
            }
        )
        summarized_result = summarize_tool_result(
            result=result,
            max_tokens=_DEFAULT_TOOL_RESULT_SUMMARY_TOKENS,
        )
        context.tool_results.append(
            {
                "step": index,
                "tool": tool_name,
                "args": safe_tool_args,
                "access_level": policy_metadata["access_level"],
                "policy": policy_metadata,
                "result": summarized_result,
                "retry": retry_metadata,
            }
        )
        _record_user_profile_step(
            tool_name=tool_name,
            raw_args=tool_args,
            safe_args=safe_tool_args,
            result=result,
            retry_metadata=retry_metadata,
        )

        if result["status"] == "success":
            steps_completed += 1
            final_output = result["message"]
            if retry_count > 0:
                emit(f"  OK {result['message']} (self-heal recovered after {retry_count} retry)", "success")
            else:
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

    _persist_self_heal_outcomes(task=task, context=context, outcomes=self_heal_outcomes)
    _record_user_profile_self_heal(task=task, outcomes=self_heal_outcomes)

    success = steps_completed > 0 and not execution_failed
    elapsed = round(time.time() - start_time, 1)

    _log_execution(
        task=task,
        success=success,
        steps_completed=steps_completed,
        format_failures=format_failures,
        retries=retries + self_heal_retries,
        final_output=final_output,
        error=None if success else "execution_error",
        elapsed_seconds=elapsed,
        router_decision=context.router_decision,
        router_confidence=context.router_confidence,
        model_used=model_used,
        steps=context.steps,
        tool_results=context.tool_results,
        plan_retries=retries,
        self_heal_retries=self_heal_retries,
        self_heal_outcomes=self_heal_outcomes,
        decomposition_used=context.decomposition_used,
        decomposed_steps=context.decomposed_steps,
        step_routes=context.step_routes,
        access_level_policy=context.access_level_policy,
    )

    icon = "OK" if success else "FAIL"
    summary = (
        f"[VOCO] {icon} Task complete in {elapsed}s. "
        f"Steps: {steps_completed}/{max_steps_to_run}. Result: {final_output}"
    )
    emit(summary, "success" if success else "error")
    return summary


_ROUTER_DIRECT_THRESHOLD = 0.85
_ROUTER_LOW_CONF_DIRECT_THRESHOLD = 0.30
_ROUTER_LOW_CONF_DIRECT_TOOLS = {
    "open_app",
    "browser_navigate",
    "browser_switch_profile",
    "browser_type",
    "browser_click",
    "browser_press_key",
    "write_in_notepad",
    "save_text_to_desktop_file",
    "search_local_paths",
    "search_in_explorer",
    "open_existing_document",
}
_ACCESS_LEVEL_BY_TOOL: dict[str, str] = {
    # L1: UI automation / app interaction
    "focus_window": "L1",
    "click_in_window": "L1",
    "get_window_state": "L1",
    "write_in_notepad": "L1",
    "type_text": "L1",
    "press_key": "L1",
    "click_at": "L1",
    "take_screenshot": "L1",
    # L2: user/admin OS + browser operations
    "browser_navigate": "L2",
    "browser_type": "L2",
    "browser_click": "L2",
    "browser_press_key": "L2",
    "browser_get_state": "L2",
    "browser_switch_profile": "L2",
    "search_local_paths": "L2",
    "search_in_explorer": "L2",
    "open_app": "L2",
    "open_existing_document": "L2",
    "open_file_with_default_app": "L2",
    "read_file": "L2",
    "write_file": "L2",
    "list_files": "L2",
    "get_system_health_snapshot": "L2",
    "list_running_processes": "L2",
    "get_network_status": "L2",
    "list_usb_devices": "L2",
    "save_text_to_desktop_file": "L2",
    "index_files": "L2",
    "index_apps": "L2",
    "search_file": "L2",
    "youtube_comment_pipeline": "L2",
    # L3: privileged/sensitive system controls
    "run_command": "L3",
    "run_powershell_command": "L3",
    "kill_process": "L3",
    "disable_usb_device": "L3",
    "add_firewall_rule": "L3",
    "read_registry": "L3",
}


def _get_tool_access_level(tool_name: str) -> str:
    return _ACCESS_LEVEL_BY_TOOL.get(str(tool_name).strip(), "L2")


_POLICY_APPROVAL_ERROR_CODE = "POLICY_APPROVAL_REQUIRED"


def _is_privileged_tool_action(tool_name: str, requires_approval: bool = False) -> bool:
    access_level = _get_tool_access_level(tool_name)
    return access_level == "L3" or bool(requires_approval)


def _build_policy_metadata(
    tool_name: str,
    requires_approval: bool,
    human_approved: bool,
    policy_scope: str,
    approval_error_code: str = "",
) -> dict:
    access_level = _get_tool_access_level(tool_name)
    return {
        "policy_scope": str(policy_scope).strip() or "L1-L3",
        "access_level": access_level,
        "privileged_action": _is_privileged_tool_action(tool_name, requires_approval=requires_approval),
        "requires_approval": bool(requires_approval),
        "human_approved": bool(human_approved),
        "approval_error_code": str(approval_error_code).strip(),
    }


def _format_policy_metadata(policy_metadata: dict) -> str:
    return (
        f"policy_scope={policy_metadata['policy_scope']}; "
        f"access_level={policy_metadata['access_level']}; "
        f"privileged_action={str(bool(policy_metadata['privileged_action'])).lower()}; "
        f"requires_approval={str(bool(policy_metadata['requires_approval'])).lower()}; "
        f"human_approved={str(bool(policy_metadata['human_approved'])).lower()}"
    )


def _format_step_reason_with_policy(reason: str, policy_metadata: dict) -> str:
    detail = reason.strip() or "Tool-first execution step."
    return f"{detail} [{_format_policy_metadata(policy_metadata)}]"


def _build_approval_required_result(tool_name: str, policy_metadata: dict) -> dict:
    message = (
        f"{_POLICY_APPROVAL_ERROR_CODE}: Tool '{tool_name}' requires human approval. "
        "Re-run with explicit approval in the task or args.human_approval=true. "
        f"{_format_policy_metadata(policy_metadata)}"
    )
    return {"status": "error", "result": None, "message": message}


def _annotate_step_reason(step_text: str, route_path: str, base_reason: str, tool_name: str) -> str:
    access_level = _get_tool_access_level(tool_name)
    detail = base_reason.strip() or "Tool-first execution step."
    return f"[{access_level}|{route_path}] {step_text} -> {detail}"


def _plan_single_step_with_llm(step_task: str, context: AgentContext) -> dict:
    """Use Gemma as per-step fallback planner for unresolved atomic steps."""
    system_prompt = build_system_prompt(context)
    memory_summary = _build_memory_summary(context=context, max_tokens=80)
    last_result = None
    if context.tool_results:
        last_tool_entry = context.tool_results[-1]
        if isinstance(last_tool_entry, dict):
            last_result = last_tool_entry.get("result")
    step_context = build_minimal_context(
        task=context.task or step_task,
        step=step_task,
        last_result=last_result,
        memory_summary=memory_summary,
    )
    raw_response = generate(system_prompt=system_prompt, user_message=step_context)
    model_used = get_last_model_used()
    status, plan = parse_response(raw_response)
    format_failures = 0
    retries = 0

    if status == "format_failure":
        format_failures += 1
        correction_signal = {
            "status": "format_failure",
            "message": "Step planner output was not valid JSON.",
            "result": raw_response,
        }
        correction_context = build_minimal_context(
            task=context.task or step_task,
            step=f"Correct planner output for step: {step_task}",
            last_result=correction_signal,
            memory_summary=memory_summary,
        )
        correction_messages = [
            {"role": "user", "content": correction_context},
            {"role": "assistant", "content": _safe_json_dumps(summarize_tool_result(correction_signal, 80))},
            {"role": "user", "content": build_correction_prompt()},
        ]
        corrected_response = generate_with_history(system_prompt, correction_messages)
        retries += 1
        model_used = get_last_model_used()
        status, plan = parse_response(corrected_response)
        _log_format_failure(
            task=step_task,
            raw_response=raw_response,
            corrected_response=corrected_response,
            correction_worked=(status == "ok"),
            model_used=model_used,
        )
        raw_response = corrected_response

    if status != "ok" or not plan:
        return {
            "ok": False,
            "error": f"Failed to generate a valid action plan for decomposed step: '{step_task}'.",
            "raw_response": raw_response[:300],
            "model_used": model_used,
            "format_failures": format_failures,
            "retries": retries,
            "plan": [],
        }

    return {
        "ok": True,
        "error": "",
        "raw_response": _truncate_text(raw_response, 500),
        "model_used": model_used,
        "format_failures": format_failures,
        "retries": retries,
        "plan": plan,
    }


_EXPLICIT_REPEAT_STEP_REGEX = re.compile(
    r"\b(?:again|once more|one more time|repeat|re-open|reopen|refocus|re-focus|retry|twice|thrice|"
    r"second time|third time|\d+\s+times?|two\s+times?|three\s+times?)\b",
    flags=re.IGNORECASE,
)


def _is_explicit_repeat_step(step_text: str) -> bool:
    return _EXPLICIT_REPEAT_STEP_REGEX.search(str(step_text or "")) is not None


def _normalize_open_focus_action_key(tool_name: str, args: dict[str, object]) -> tuple[str, str] | None:
    normalized_tool = str(tool_name or "").strip().lower()
    if not normalized_tool:
        return None

    def _normalize_token(value: object) -> str:
        token = str(value or "").strip().strip("\"'").lower()
        return re.sub(r"\s+", " ", token)

    if normalized_tool == "open_app":
        app_name = _normalize_token(args.get("app_name"))
        if app_name:
            return ("open", f"app:{app_name}")
        return None

    if normalized_tool == "focus_window":
        window_title = _normalize_token(args.get("window_title"))
        if window_title:
            return ("focus", f"window:{window_title}")
        return None

    if normalized_tool == "browser_navigate":
        url = _normalize_token(args.get("url"))
        if not url:
            return None
        browser = _normalize_token(args.get("browser")) or "default"
        normalized_url = re.sub(r"^https?://", "", url).rstrip("/")
        return ("open", f"browser:{browser}:{normalized_url}")

    if normalized_tool == "browser_switch_profile":
        profile_mode = _normalize_token(args.get("profile_mode"))
        if not profile_mode:
            return None
        browser = _normalize_token(args.get("browser")) or "default"
        return ("open", f"profile:{browser}:{profile_mode}")

    if normalized_tool == "open_existing_document":
        path = _normalize_token(args.get("path"))
        if path:
            return ("open", f"document:{path}")
        extension = _normalize_token(args.get("extension"))
        query = _normalize_token(args.get("query"))
        if extension or query:
            return ("open", f"document:{extension}:{query}")
        return None

    return None


_BROWSER_OPEN_APP_ALIASES = frozenset(
    {
        "browser",
        "chrome",
        "google chrome",
        "msedge",
        "edge",
        "microsoft edge",
        "firefox",
        "chromium",
    }
)


def _is_browser_open_app_step(tool_name: str, args: dict[str, object]) -> bool:
    if str(tool_name or "").strip().lower() != "open_app":
        return False
    app_name = str(args.get("app_name", "")).strip().strip("\"'").lower()
    app_name = re.sub(r"\s+", " ", app_name)
    return app_name in _BROWSER_OPEN_APP_ALIASES


def _has_future_browser_navigation_step(plan: list[dict], start_index: int) -> bool:
    for candidate in plan[start_index + 1 :]:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("tool", "")).strip().lower() == "browser_navigate":
            return True
    return False


def _cleanup_decomposed_plan_steps(plan: list[dict], step_sources: list[str]) -> tuple[list[dict], int]:
    cleaned_plan: list[dict] = []
    suppressed_steps = 0
    previous_open_focus_key: tuple[str, str] | None = None

    for index, raw_step in enumerate(plan):
        if not isinstance(raw_step, dict):
            cleaned_plan.append(raw_step)
            previous_open_focus_key = None
            continue

        tool_name = str(raw_step.get("tool", "")).strip()
        raw_args = raw_step.get("args", {})
        args = raw_args if isinstance(raw_args, dict) else {}
        step = raw_step if isinstance(raw_args, dict) else {**raw_step, "args": args}
        source_step = step_sources[index] if index < len(step_sources) else ""

        if (
            _is_browser_open_app_step(tool_name=tool_name, args=args)
            and _has_future_browser_navigation_step(plan=plan, start_index=index)
            and not _is_explicit_repeat_step(source_step)
        ):
            suppressed_steps += 1
            continue

        action_key = _normalize_open_focus_action_key(tool_name=tool_name, args=args)
        if action_key is None:
            cleaned_plan.append(step)
            previous_open_focus_key = None
            continue

        if previous_open_focus_key == action_key and not _is_explicit_repeat_step(source_step):
            suppressed_steps += 1
            continue

        cleaned_plan.append(step)
        previous_open_focus_key = action_key

    return cleaned_plan, suppressed_steps


def _llm_decompose_steps(task_text: str, max_steps: int) -> str:
    if not check_ollama_running():
        return ""
    prompt = (
        "Break the request into atomic executable steps.\n"
        f"Return ONLY a numbered list with no more than {max_steps} steps.\n"
        "Rules:\n"
        "1) Keep each step as one direct action.\n"
        "2) Preserve critical details (profile names, file names, destinations).\n"
        "3) Do not add commentary, markdown, or JSON.\n\n"
        f"Request: {task_text}"
    )
    ok, response = generate_conversation(user_message=prompt, temperature=0.0)
    if not ok:
        return ""
    return str(response).strip()


def _build_tool_first_hybrid_plan(task: str, context: AgentContext, emit) -> dict | None:
    """
    Build a tool-first plan:
    1) split non-trivial tasks
    2) route each step via ML router
    3) execute direct tool for high-confidence routes
    4) fallback to local fast-path
    5) fallback unresolved steps to Gemma planner
    """
    if _is_conversational_prompt(task):
        return None
    if not needs_decomposition(task):
        return None

    split_steps = decompose_task(
        task,
        max_steps=MAX_STEPS,
        llm_decomposer=_llm_decompose_steps,
        allow_llm_fallback=True,
    )
    if len(split_steps) <= 1:
        return None

    plan: list[dict] = []
    route_trace: list[dict] = []
    plan_step_sources: list[str] = []
    unresolved_steps: list[str] = []
    format_failures = 0
    plan_retries = 0
    last_model_used = ""

    def _append_plan_step(step_payload: dict, source_step_text: str) -> None:
        plan.append(step_payload)
        plan_step_sources.append(source_step_text)

    def _finalize_bundle(confidence: float, error: str = "") -> dict:
        cleaned_plan, suppressed_steps = _cleanup_decomposed_plan_steps(
            plan=plan,
            step_sources=plan_step_sources,
        )
        if suppressed_steps:
            emit(
                f"[VOCO] Decomposition cleanup removed {suppressed_steps} redundant open/focus step(s).",
                "info",
            )
        return {
            "plan": cleaned_plan,
            "split_steps": split_steps,
            "route_trace": route_trace,
            "decomposition_used": True,
            "confidence": confidence,
            "model_used": last_model_used,
            "format_failures": format_failures,
            "plan_retries": plan_retries,
            "cleanup_suppressed_steps": suppressed_steps,
            "error": error,
        }

    for step_index, step_text in enumerate(split_steps, start=1):
        route = predict_route(step_text)
        intent = str(route.get("intent", "unknown")).strip()
        confidence = float(route.get("confidence", 0.0) or 0.0)
        tool_name = str(route.get("tool", "")).strip()
        args = route.get("args", {})
        missing_args = route.get("missing_args", [])
        rejected_reason = str(route.get("rejected_reason", "")).strip()

        if not isinstance(args, dict):
            args = {}
        if not isinstance(missing_args, list):
            missing_args = []

        route_path = "router_unresolved"
        has_valid_route = bool(tool_name and tool_name in TOOL_REGISTRY and not missing_args and not rejected_reason)
        can_route_direct = bool(
            has_valid_route
            and (
                confidence >= _ROUTER_DIRECT_THRESHOLD
                or (
                    confidence >= _ROUTER_LOW_CONF_DIRECT_THRESHOLD
                    and tool_name in _ROUTER_LOW_CONF_DIRECT_TOOLS
                )
            )
        )

        if can_route_direct:
            route_path = "router_direct" if confidence >= _ROUTER_DIRECT_THRESHOLD else "router_direct_low_conf"
            direct_args = dict(args)
            if tool_name == "browser_type":
                if _browser_submit_requested(step_text, include_search=True):
                    direct_args.setdefault("submit", True)
                text_value = str(direct_args.get("text", ""))
                if text_value:
                    normalized_text = _normalize_browser_multiline_text(text_value)
                    if normalized_text:
                        direct_args["text"] = normalized_text
                if _browser_multiline_requested(step_text) or "\n" in str(direct_args.get("text", "")):
                    direct_args.setdefault("multiline", True)
                    direct_args.setdefault("newline_mode", "shift_enter")
            args = direct_args
            _append_plan_step(
                {
                    "tool": tool_name,
                    "args": args,
                    "reason": _annotate_step_reason(
                        step_text=step_text,
                        route_path=route_path,
                        base_reason=f"intent={intent} confidence={confidence:.2f}",
                        tool_name=tool_name,
                    ),
                },
                source_step_text=step_text,
            )
        else:
            fallback_plan = _build_local_fastpath_plan(step_text)
            if fallback_plan is not None:
                route_path = "local_fastpath_fallback"
                for fallback_step in fallback_plan:
                    fallback_tool = str(fallback_step.get("tool", "")).strip()
                    fallback_args = fallback_step.get("args", {})
                    if not isinstance(fallback_args, dict):
                        fallback_args = {}
                    fallback_reason = str(fallback_step.get("reason", "")).strip()
                    _append_plan_step(
                        {
                            "tool": fallback_tool,
                            "args": fallback_args,
                            "reason": _annotate_step_reason(
                                step_text=step_text,
                                route_path=route_path,
                                base_reason=fallback_reason,
                                tool_name=fallback_tool,
                            ),
                        },
                        source_step_text=step_text,
                    )
            elif tool_name and tool_name in TOOL_REGISTRY and missing_args:
                route_path = "router_missing_args_fallback"
                unresolved_steps.append(step_text)
            else:
                unresolved_steps.append(step_text)

        route_trace.append(
            {
                "step": step_index,
                "text": step_text,
                "intent": intent,
                "confidence": round(confidence, 4),
                "tool": tool_name,
                "args": args,
                "missing_args": missing_args,
                "rejected_reason": rejected_reason,
                "path": route_path,
            }
        )

    if unresolved_steps:
        if not check_ollama_running():
            return _finalize_bundle(
                confidence=0.82,
                error=(
                    "Tool-first decomposition found unresolved steps but Gemma fallback is unavailable. "
                    f"Run: ollama serve && ollama pull {OLLAMA_MODEL}"
                ),
            )

        for unresolved in unresolved_steps:
            emit(f"[VOCO] Router uncertain for step, using Gemma fallback: {unresolved}", "info")
            llm_step = _plan_single_step_with_llm(step_task=unresolved, context=context)
            format_failures += int(llm_step.get("format_failures", 0))
            plan_retries += int(llm_step.get("retries", 0))
            model_candidate = str(llm_step.get("model_used", "")).strip()
            if model_candidate:
                last_model_used = model_candidate

            if not bool(llm_step.get("ok")):
                return _finalize_bundle(
                    confidence=0.8,
                    error=str(llm_step.get("error", "Gemma fallback step planning failed.")),
                )

            for llm_plan_step in llm_step.get("plan", []):
                if not isinstance(llm_plan_step, dict):
                    continue
                llm_tool = str(llm_plan_step.get("tool", "")).strip()
                llm_args = llm_plan_step.get("args", {})
                if not isinstance(llm_args, dict):
                    llm_args = {}
                llm_reason = str(llm_plan_step.get("reason", "")).strip()
                _append_plan_step(
                    {
                        "tool": llm_tool,
                        "args": llm_args,
                        "reason": _annotate_step_reason(
                            step_text=unresolved,
                            route_path="llm_fallback",
                            base_reason=llm_reason,
                            tool_name=llm_tool,
                        ),
                    },
                    source_step_text=unresolved,
                )

    average_confidence = 0.0
    if route_trace:
        confidence_values = [float(item.get("confidence", 0.0)) for item in route_trace]
        average_confidence = sum(confidence_values) / max(1, len(confidence_values))

    return _finalize_bundle(confidence=max(0.8, min(1.0, average_confidence)), error="")


_SENSITIVE_ARG_TOKENS = (
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
_REDACTED_ARG_VALUE = "[REDACTED]"


def _is_sensitive_arg_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    return any(token in normalized for token in _SENSITIVE_ARG_TOKENS)


def _sanitize_nested_arg_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (_REDACTED_ARG_VALUE if _is_sensitive_arg_key(str(key)) else _sanitize_nested_arg_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_nested_arg_value(item) for item in value]
    return value


def _sanitize_tool_args(tool_name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return {}

    normalized_tool = str(tool_name or "").strip().lower()
    sanitized: dict = {}
    for key, value in args.items():
        normalized_key = str(key).strip().lower()
        redact_value = _is_sensitive_arg_key(normalized_key)
        if normalized_tool == "update_user_profile" and normalized_key == "value":
            redact_value = True
        if redact_value:
            sanitized[key] = _REDACTED_ARG_VALUE
            continue
        sanitized[key] = _sanitize_nested_arg_value(value)
    return sanitized


def _format_args_preview(tool_name: str, args: dict, max_length: int = 60) -> str:
    if not args:
        return ""
    safe_args = _sanitize_tool_args(tool_name=tool_name, args=args)
    parts = [f"{key}={repr(value)[:20]}" for key, value in safe_args.items()]
    preview = ", ".join(parts)
    if len(preview) > max_length:
        return preview[:max_length] + "..."
    return preview


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_PROFILE_FILE_ACTIVITY_TOOLS = {
    "read_file",
    "write_file",
    "list_files",
    "open_existing_document",
    "open_file_with_default_app",
    "search_file",
    "search_local_paths",
    "save_text_to_desktop_file",
    "index_files",
}
_PROFILE_FILE_ACTIVITY_ARG_KEYS = (
    "path",
    "file_path",
    "source_path",
    "destination_path",
    "directory",
    "output_path",
    "target_path",
    "filename",
)


def _load_user_profile_class() -> type | None:
    global _user_profile_class_attempted, _user_profile_class
    if _user_profile_class is not None:
        return _user_profile_class
    if _user_profile_class_attempted:
        return None

    _user_profile_class_attempted = True
    if not _USER_PROFILE_MODULE_PATH.exists():
        return None

    try:
        spec = importlib.util.spec_from_file_location("voco_user_profile", _USER_PROFILE_MODULE_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = getattr(module, "UserProfile", None)
        if isinstance(candidate, type):
            _user_profile_class = candidate
    except Exception:
        _user_profile_class = None
    return _user_profile_class


def _get_user_profile_store() -> object | None:
    global _user_profile_store_attempted, _user_profile_store
    if _user_profile_store is not None:
        return _user_profile_store
    if _user_profile_store_attempted:
        return None

    _user_profile_store_attempted = True
    profile_class = _load_user_profile_class()
    if profile_class is None:
        return None
    try:
        _user_profile_store = profile_class()
    except Exception:
        _user_profile_store = None
    return _user_profile_store


def _load_fs_watcher_module() -> object | None:
    global _fs_watcher_module_attempted, _fs_watcher_module
    if _fs_watcher_module is not None:
        return _fs_watcher_module
    if _fs_watcher_module_attempted:
        return None

    _fs_watcher_module_attempted = True
    if not _FS_WATCHER_MODULE_PATH.exists():
        return None

    try:
        spec = importlib.util.spec_from_file_location("voco_fs_watcher", _FS_WATCHER_MODULE_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _fs_watcher_module = module
    except Exception:
        _fs_watcher_module = None
    return _fs_watcher_module


def _observer_is_alive(observer: object | None) -> bool:
    if observer is None:
        return False
    is_alive = getattr(observer, "is_alive", None)
    if callable(is_alive):
        try:
            return bool(is_alive())
        except Exception:
            return False
    return True


def _ensure_fs_watcher_started() -> object | None:
    global _fs_watcher_observer
    if _observer_is_alive(_fs_watcher_observer):
        return _fs_watcher_observer

    module = _load_fs_watcher_module()
    if module is None:
        return None

    starter = getattr(module, "start_filesystem_watcher", None)
    if not callable(starter):
        return None
    try:
        observer = starter()
    except Exception:
        return None

    if _observer_is_alive(observer):
        _fs_watcher_observer = observer
        return observer
    return None


def start_fs_watcher() -> object | None:
    return _ensure_fs_watcher_started()


def stop_fs_watcher(observer: object | None = None) -> None:
    global _fs_watcher_observer
    module = _load_fs_watcher_module()
    target = observer if observer is not None else _fs_watcher_observer
    if target is None:
        return

    if module is not None:
        stopper = getattr(module, "stop_filesystem_watcher", None)
        if callable(stopper):
            try:
                stopper(target)
            except Exception:
                pass
    elif _observer_is_alive(target):
        stop_method = getattr(target, "stop", None)
        if callable(stop_method):
            try:
                stop_method()
            except Exception:
                pass
        join_method = getattr(target, "join", None)
        if callable(join_method):
            try:
                join_method(timeout=3)
            except Exception:
                pass

    if target is _fs_watcher_observer or observer is None:
        _fs_watcher_observer = None


_TEACH_MODE_ENDPOINT = "teach_mode_store_correction"


def _try_user_profile_method(profile: object, method_name: str, **kwargs) -> tuple[bool, object | None]:
    method = getattr(profile, method_name, None)
    if not callable(method):
        return False, None
    try:
        return True, method(**kwargs)
    except Exception:
        return False, None


def _call_user_profile_method(profile: object, method_name: str, **kwargs) -> None:
    _try_user_profile_method(profile, method_name, **kwargs)


def _build_retry_recipe_key(failure_class: str, tool_name: str) -> str:
    normalized_failure = _normalize_whitespace(str(failure_class or "")).lower() or "unknown"
    normalized_tool = _normalize_whitespace(str(tool_name or "")).lower() or "unknown"
    return _truncate_text(f"{normalized_failure}|{normalized_tool}", 280)


def _collect_profile_retry_hints(
    profile: object,
    *,
    failure_class: str,
    tool_name: str,
    limit: int = 3,
) -> list[dict[str, str]]:
    normalized_failure = _normalize_whitespace(failure_class).lower()
    normalized_tool = _normalize_whitespace(tool_name).lower()
    max_hints = max(1, limit)
    hints: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add_hint(text: object, source: str) -> None:
        hint = _truncate_text(_normalize_whitespace(str(text or "")), 260)
        if not hint:
            return
        signature = hint.lower()
        if signature in seen:
            return
        seen.add(signature)
        hints.append({"hint": hint, "source": source})

    profile_queries: list[dict[str, object]] = []
    if normalized_failure and normalized_tool:
        profile_queries.append({"failure_class": normalized_failure, "tool_name": normalized_tool})
    if normalized_failure:
        profile_queries.append({"failure_class": normalized_failure})
    if normalized_tool:
        profile_queries.append({"tool_name": normalized_tool})

    for query in profile_queries:
        ok, rows = _try_user_profile_method(profile, "get_failure_memory", limit=8, **query)
        if not ok or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            _add_hint(row.get("known_fix"), "failure_memory")
            if len(hints) >= max_hints:
                return hints[:max_hints]

    ok, recipes = _try_user_profile_method(profile, "get_learned_recipes", limit=30)
    if not ok or not isinstance(recipes, list):
        return hints[:max_hints]

    recipe_key = _build_retry_recipe_key(normalized_failure, normalized_tool)
    for recipe in recipes:
        if not isinstance(recipe, dict):
            continue
        key = _normalize_whitespace(str(recipe.get("recipe_key", ""))).lower()
        if key != recipe_key:
            continue
        _add_hint(recipe.get("recipe_text"), "learned_recipe")
        if len(hints) >= max_hints:
            return hints[:max_hints]

    if hints:
        return hints[:max_hints]

    for recipe in recipes:
        if not isinstance(recipe, dict):
            continue
        key = _normalize_whitespace(str(recipe.get("recipe_key", ""))).lower()
        if not key:
            continue
        matches_failure = bool(normalized_failure and key.startswith(f"{normalized_failure}|"))
        matches_tool = bool(normalized_tool and key.endswith(f"|{normalized_tool}"))
        if not (matches_failure or matches_tool):
            continue
        _add_hint(recipe.get("recipe_text"), "learned_recipe")
        if len(hints) >= max_hints:
            return hints[:max_hints]

    return hints[:max_hints]


def _build_retry_correction_context(
    *,
    tool_name: str,
    failure_class: str,
    known_fix: str,
    error_message: str,
) -> dict[str, object]:
    normalized_tool = _normalize_whitespace(str(tool_name or "")).lower() or "unknown"
    normalized_failure = _normalize_whitespace(str(failure_class or "")).lower() or "unknown"
    fallback_fix = _truncate_text(_normalize_whitespace(str(known_fix or "")), 260)
    context: dict[str, object] = {
        "failure_class": normalized_failure,
        "tool_name": normalized_tool,
        "base_known_fix": fallback_fix,
        "selected_hint": fallback_fix,
        "selected_hint_source": "builtin" if fallback_fix else "none",
        "profile_hints": [],
        "profile_match_count": 0,
        "error_excerpt": _truncate_text(_normalize_whitespace(str(error_message or "")), 220),
    }

    profile = _get_user_profile_store()
    if profile is None:
        return context

    profile_hints = _collect_profile_retry_hints(
        profile,
        failure_class=normalized_failure,
        tool_name=normalized_tool,
        limit=3,
    )
    hint_values = [str(item.get("hint", "")).strip() for item in profile_hints if isinstance(item, dict)]
    hint_values = [hint for hint in hint_values if hint]
    context["profile_hints"] = hint_values
    context["profile_match_count"] = len(hint_values)

    if hint_values:
        context["selected_hint"] = hint_values[0]
        context["selected_hint_source"] = str(profile_hints[0].get("source", "profile")).strip() or "profile"

    return context


def _build_learn_teach_affordance(
    *,
    tool_name: str,
    failure_class: str,
    retry_count: int,
    max_retries: int,
    correction_context: dict[str, object] | None = None,
) -> dict[str, object]:
    context = correction_context if isinstance(correction_context, dict) else {}
    normalized_tool = _normalize_whitespace(str(tool_name or "")).lower() or "unknown"
    normalized_failure = _normalize_whitespace(str(failure_class or "")).lower() or "unknown"
    suggested_hint = _truncate_text(_normalize_whitespace(str(context.get("selected_hint", ""))), 260)
    hint_source = _normalize_whitespace(str(context.get("selected_hint_source", ""))) or "builtin"
    return {
        "available": True,
        "mode": "teach_mode",
        "endpoint": _TEACH_MODE_ENDPOINT,
        "recipe_key": _build_retry_recipe_key(normalized_failure, normalized_tool),
        "failure_class": normalized_failure,
        "tool_name": normalized_tool,
        "suggested_correction": suggested_hint,
        "hint_source": hint_source,
        "retry_count": max(0, _safe_int(retry_count, 0)),
        "max_retries": max(0, _safe_int(max_retries, 0)),
    }


def teach_mode_store_correction(
    failure_class: str,
    tool_name: str,
    correction_text: str,
    *,
    success: bool | None = None,
    metadata: dict | None = None,
) -> dict:
    profile = _get_user_profile_store()
    if profile is None:
        return {
            "status": "error",
            "result": None,
            "message": "Teach-mode storage unavailable: UserProfile store not initialized.",
        }

    normalized_failure = _normalize_whitespace(str(failure_class or "")).lower() or "unknown"
    normalized_tool = _normalize_whitespace(str(tool_name or "")).lower() or "unknown"
    normalized_correction = _truncate_text(_normalize_whitespace(str(correction_text or "")), 4000)
    if not normalized_correction:
        return {
            "status": "error",
            "result": None,
            "message": "Teach-mode storage requires a non-empty correction_text.",
        }

    payload_metadata = {
        "source": "teach_mode_scaffold",
        "failure_class": normalized_failure,
        "tool_name": normalized_tool,
    }
    if isinstance(metadata, dict):
        payload_metadata.update(metadata)

    call_succeeded = False
    stored_payload: object | None = None
    if hasattr(profile, "store_teach_mode_entry"):
        call_succeeded, stored_payload = _try_user_profile_method(
            profile,
            "store_teach_mode_entry",
            failure_class=normalized_failure,
            tool_name=normalized_tool,
            correction_text=normalized_correction,
            success=success,
            metadata=payload_metadata,
            record_failure_event=False,
        )

    recipe_key = _build_retry_recipe_key(normalized_failure, normalized_tool)
    if not call_succeeded:
        call_succeeded, _ = _try_user_profile_method(
            profile,
            "record_learned_recipe",
            recipe_key=recipe_key,
            recipe_text=normalized_correction,
            success=success,
            metadata=payload_metadata,
        )

    if not call_succeeded:
        return {
            "status": "error",
            "result": None,
            "message": "Teach-mode storage failed while writing correction entry to UserProfile.",
        }

    result_payload: dict[str, object] = {
        "recipe_key": recipe_key,
        "failure_class": normalized_failure,
        "tool_name": normalized_tool,
        "stored_correction": normalized_correction,
        "source": "teach_mode_scaffold",
    }
    if isinstance(stored_payload, dict):
        for key in ("updated_at", "last_outcome", "record_failure_event"):
            value = stored_payload.get(key)
            if value is not None:
                result_payload[key] = value

    return {
        "status": "success",
        "result": result_payload,
        "message": "Teach-mode correction stored in UserProfile.",
    }


def _extract_profile_file_paths(tool_name: str, args: dict) -> list[str]:
    normalized_tool = str(tool_name or "").strip().lower()
    if normalized_tool not in _PROFILE_FILE_ACTIVITY_TOOLS:
        return []

    paths: list[str] = []
    for key in _PROFILE_FILE_ACTIVITY_ARG_KEYS:
        value = args.get(key)
        candidates: list[str] = []
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple)):
            candidates = [item for item in value if isinstance(item, str)]
        for candidate in candidates:
            normalized_path = _truncate_text(str(candidate).strip(), 1200)
            if normalized_path and normalized_path not in paths:
                paths.append(normalized_path)
    return paths[:5]


def _extract_profile_app_event(tool_name: str, args: dict) -> tuple[str, str] | None:
    normalized_tool = str(tool_name or "").strip().lower()
    if normalized_tool == "open_app":
        app_name = _normalize_whitespace(str(args.get("app_name") or args.get("name") or ""))
        if app_name:
            return app_name, "open"
        return None

    if normalized_tool.startswith("browser_"):
        browser = _normalize_whitespace(str(args.get("browser") or "browser"))
        action = normalized_tool.removeprefix("browser_") or normalized_tool
        return browser, action

    if normalized_tool in {"focus_window", "get_window_state", "click_in_window", "type_text"}:
        window_name = _normalize_whitespace(str(args.get("window_title") or args.get("title") or ""))
        if window_name:
            return window_name, normalized_tool
    return None


def _record_user_profile_task(task: str) -> None:
    profile = _get_user_profile_store()
    if profile is None:
        return

    normalized_task = _truncate_text(_normalize_whitespace(task), 1200)
    if not normalized_task:
        return
    _call_user_profile_method(
        profile,
        "record_command",
        command=normalized_task,
        status="task_received",
        tool_name="orchestrator.run",
        metadata={"source": "run"},
    )


def _record_user_profile_step(
    tool_name: str,
    raw_args: dict,
    safe_args: dict,
    result: dict,
    retry_metadata: dict,
) -> None:
    profile = _get_user_profile_store()
    if profile is None:
        return

    normalized_tool = str(tool_name or "").strip()
    if not normalized_tool:
        return
    normalized_status = _normalize_whitespace(str(result.get("status", ""))).lower() or "unknown"
    normalized_message = _truncate_text(_normalize_whitespace(str(result.get("message", ""))), 320)
    safe_args_payload = safe_args if isinstance(safe_args, dict) else {}
    safe_args_preview = _truncate_text(_safe_json_dumps(safe_args_payload), 900)
    command_text = normalized_tool
    if safe_args_preview and safe_args_preview != "{}":
        command_text = f"{normalized_tool} {safe_args_preview}"

    retry_payload = retry_metadata if isinstance(retry_metadata, dict) else {}
    _call_user_profile_method(
        profile,
        "record_command",
        command=command_text,
        status=normalized_status,
        tool_name=normalized_tool,
        metadata={
            "message": normalized_message,
            "retry_count": max(0, _safe_int(retry_payload.get("retry_count"), 0)),
            "retries_exhausted": bool(retry_payload.get("retries_exhausted")),
        },
    )

    args_payload = raw_args if isinstance(raw_args, dict) else {}
    for candidate_path in _extract_profile_file_paths(normalized_tool, args_payload):
        _call_user_profile_method(
            profile,
            "record_file_activity",
            path=candidate_path,
            action=normalized_tool,
            tool_name=normalized_tool,
            status=normalized_status,
            metadata={"message": normalized_message},
        )

    app_event = _extract_profile_app_event(normalized_tool, args_payload)
    if app_event is not None:
        app_name, app_action = app_event
        _call_user_profile_method(
            profile,
            "record_app_usage",
            app_name=app_name,
            action=app_action,
            tool_name=normalized_tool,
            status=normalized_status,
            metadata={"message": normalized_message},
        )

    if normalized_tool.lower() == "update_user_profile" and normalized_status == "success":
        pref_key = _normalize_whitespace(str(args_payload.get("key") or ""))
        if pref_key:
            raw_pref_value = args_payload.get("value")
            if _is_sensitive_arg_key(pref_key):
                pref_value = _REDACTED_ARG_VALUE
            else:
                pref_value = _truncate_text(_normalize_whitespace(str(raw_pref_value or "")), 400)
            _call_user_profile_method(
                profile,
                "set_preference",
                key=pref_key,
                value=pref_value,
                metadata={"source": "update_user_profile"},
            )

    if normalized_status in {"error", "failure"}:
        failure_class = _normalize_whitespace(str(retry_payload.get("trigger_failure_class", "")))
        known_fix = _normalize_whitespace(str(retry_payload.get("known_fix", "")))
        _call_user_profile_method(
            profile,
            "record_failure",
            failure_class=failure_class or normalized_status,
            message=normalized_message or "tool execution failed",
            tool_name=normalized_tool,
            known_fix=known_fix,
            metadata={"args": safe_args_payload},
        )


def _record_user_profile_self_heal(task: str, outcomes: list[dict]) -> None:
    if not outcomes:
        return

    profile = _get_user_profile_store()
    if profile is None:
        return

    normalized_task = _truncate_text(_normalize_whitespace(task), 240)
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        known_fix = _normalize_whitespace(str(outcome.get("known_fix", "")))
        if not known_fix:
            continue
        failure_class = _normalize_whitespace(str(outcome.get("trigger_failure_class", ""))) or "unknown"
        tool_name = _normalize_whitespace(str(outcome.get("tool", ""))) or "unknown"
        recipe_key = _truncate_text(f"{failure_class}|{tool_name}", 280)
        _call_user_profile_method(
            profile,
            "record_learned_recipe",
            recipe_key=recipe_key,
            recipe_text=known_fix,
            success=bool(outcome.get("resolved")),
            metadata={
                "task": normalized_task,
                "step": _safe_int(outcome.get("step"), 0),
                "retry_count": max(0, _safe_int(outcome.get("retry_count"), 0)),
                "retries_exhausted": bool(outcome.get("retries_exhausted")),
            },
        )


def _classify_tool_failure(tool_name: str, result: dict) -> dict[str, object]:
    status = str(result.get("status", "")).strip().lower()
    if status == "success":
        return {"class": "none", "recoverable": False, "known_fix": ""}

    message = str(result.get("message", ""))
    text = message.lower()

    if (
        "target page, context or browser has been closed" in text
        or ("has been closed" in text and any(token in text for token in ("browser", "context", "page")))
        or "browser context closed" in text
    ):
        return {
            "class": "transient_browser_context_closed",
            "recoverable": True,
            "known_fix": "Re-open browser context/page and retry the same action.",
        }

    if "timeout" in text or "timed out" in text:
        return {
            "class": "timeout",
            "recoverable": True,
            "known_fix": "Retry after waiting for app/page readiness.",
        }

    if (
        "selector not found" in text
        or "no node found for selector" in text
        or ("element" in text and "not found" in text)
    ):
        return {
            "class": "selector_not_found",
            "recoverable": False,
            "known_fix": "Refresh state (browser_get_state/get_window_state) before selecting an element.",
        }

    if (
        "application not found" in text
        or "executable not found" in text
        or "is unavailable on this platform" in text
        or "app unavailable" in text
    ):
        return {
            "class": "app_unavailable",
            "recoverable": False,
            "known_fix": "Install, open, or re-index the required application before retrying.",
        }

    if (
        "permission denied" in text
        or "access is denied" in text
        or "requires human approval" in text
        or "administrator privileges" in text
        or "not permitted" in text
    ):
        return {
            "class": "permission_denied",
            "recoverable": False,
            "known_fix": "Provide explicit approval/elevation and retry once authorized.",
        }

    _ = tool_name
    return {
        "class": "unknown",
        "recoverable": False,
        "known_fix": "Inspect error details and adjust the plan manually.",
    }


def _sanitize_retry_metadata(raw_retry: dict | None) -> dict:
    if not isinstance(raw_retry, dict):
        return {
            "max_retries": 0,
            "retry_count": 0,
            "retries_exhausted": False,
            "trigger_failure_class": "",
            "known_fix": "",
            "attempts": [],
        }

    attempts: list[dict] = []
    raw_attempts = raw_retry.get("attempts")
    if isinstance(raw_attempts, list):
        for fallback_index, item in enumerate(raw_attempts, start=1):
            if not isinstance(item, dict):
                continue
            attempts.append(
                {
                    "attempt": _safe_int(item.get("attempt"), fallback_index),
                    "status": str(item.get("status", "")).strip().lower() or "unknown",
                    "message": str(item.get("message", "")).strip()[:300],
                    "classification": str(item.get("classification", "")).strip() or "unknown",
                    "recoverable": bool(item.get("recoverable")),
                    "known_fix": str(item.get("known_fix", "")).strip(),
                    "hint_source": str(item.get("hint_source", "")).strip(),
                    "profile_hint_count": max(0, _safe_int(item.get("profile_hint_count"), 0)),
                    "will_retry": bool(item.get("will_retry")),
                }
            )

    normalized = {
        "max_retries": max(0, _safe_int(raw_retry.get("max_retries"), 0)),
        "retry_count": max(0, _safe_int(raw_retry.get("retry_count"), 0)),
        "retries_exhausted": bool(raw_retry.get("retries_exhausted")),
        "trigger_failure_class": str(raw_retry.get("trigger_failure_class", "")).strip(),
        "known_fix": str(raw_retry.get("known_fix", "")).strip(),
        "attempts": attempts,
    }
    exhausted_message = str(raw_retry.get("exhausted_message", "")).strip()
    if exhausted_message:
        normalized["exhausted_message"] = exhausted_message[:300]

    correction_context = raw_retry.get("correction_context")
    if isinstance(correction_context, dict):
        profile_hints = correction_context.get("profile_hints")
        normalized_hints: list[str] = []
        if isinstance(profile_hints, list):
            for hint in profile_hints[:3]:
                normalized_hint = _truncate_text(_normalize_whitespace(str(hint or "")), 260)
                if normalized_hint:
                    normalized_hints.append(normalized_hint)
        normalized["correction_context"] = {
            "failure_class": str(correction_context.get("failure_class", "")).strip(),
            "tool_name": str(correction_context.get("tool_name", "")).strip(),
            "base_known_fix": _truncate_text(
                _normalize_whitespace(str(correction_context.get("base_known_fix", ""))),
                260,
            ),
            "selected_hint": _truncate_text(
                _normalize_whitespace(str(correction_context.get("selected_hint", ""))),
                260,
            ),
            "selected_hint_source": str(correction_context.get("selected_hint_source", "")).strip(),
            "profile_hints": normalized_hints,
            "profile_match_count": max(0, _safe_int(correction_context.get("profile_match_count"), 0)),
            "error_excerpt": _truncate_text(
                _normalize_whitespace(str(correction_context.get("error_excerpt", ""))),
                220,
            ),
        }

    learn_teach = raw_retry.get("learn_teach_affordance")
    if isinstance(learn_teach, dict):
        normalized["learn_teach_affordance"] = {
            "available": bool(learn_teach.get("available")),
            "mode": str(learn_teach.get("mode", "")).strip(),
            "endpoint": str(learn_teach.get("endpoint", "")).strip(),
            "recipe_key": str(learn_teach.get("recipe_key", "")).strip(),
            "failure_class": str(learn_teach.get("failure_class", "")).strip(),
            "tool_name": str(learn_teach.get("tool_name", "")).strip(),
            "suggested_correction": _truncate_text(
                _normalize_whitespace(str(learn_teach.get("suggested_correction", ""))),
                260,
            ),
            "hint_source": str(learn_teach.get("hint_source", "")).strip(),
            "retry_count": max(0, _safe_int(learn_teach.get("retry_count"), 0)),
            "max_retries": max(0, _safe_int(learn_teach.get("max_retries"), 0)),
        }
    return normalized


def _sanitize_policy_metadata(raw_policy: dict | None) -> dict:
    if not isinstance(raw_policy, dict):
        return {
            "policy_scope": "L1-L3",
            "access_level": "L2",
            "privileged_action": False,
            "requires_approval": False,
            "human_approved": False,
            "approval_error_code": "",
        }

    return {
        "policy_scope": str(raw_policy.get("policy_scope", "L1-L3")).strip() or "L1-L3",
        "access_level": str(raw_policy.get("access_level", "L2")).strip() or "L2",
        "privileged_action": bool(raw_policy.get("privileged_action")),
        "requires_approval": bool(raw_policy.get("requires_approval")),
        "human_approved": bool(raw_policy.get("human_approved")),
        "approval_error_code": str(raw_policy.get("approval_error_code", "")).strip(),
    }


def _build_tool_results_log(tool_results: list[dict] | None) -> list[dict]:
    records: list[dict] = []
    for fallback_index, item in enumerate(tool_results or [], start=1):
        if not isinstance(item, dict):
            continue
        payload = item.get("result", {})
        status = "unknown"
        message = ""
        result_preview = ""
        result_debug: dict[str, object] = {}
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).strip().lower() or "unknown"
            message = str(payload.get("message", "")).strip()
            payload_result = payload.get("result")
            if payload_result is not None:
                result_preview = _truncate_text(
                    _normalize_whitespace(_safe_json_dumps(payload_result)),
                    240,
                )
            debug_payload = payload.get("debug")
            if isinstance(debug_payload, dict):
                result_debug = {
                    "raw_type": str(debug_payload.get("raw_type", "")).strip(),
                    "approx_chars": max(0, _safe_int(debug_payload.get("approx_chars"), 0)),
                    "summary_chars": max(0, _safe_int(debug_payload.get("summary_chars"), 0)),
                    "truncated": bool(debug_payload.get("truncated")),
                }
        policy = _sanitize_policy_metadata(item.get("policy"))
        records.append(
            {
                "step": _safe_int(item.get("step"), fallback_index),
                "tool": str(item.get("tool", "")).strip(),
                "status": status,
                "message": message[:300],
                "result_preview": result_preview,
                "result_debug": result_debug,
                "access_level": policy["access_level"],
                "policy": policy,
                "retry": _sanitize_retry_metadata(item.get("retry")),
            }
        )
    return records


def _persist_self_heal_outcomes(task: str, context: AgentContext, outcomes: list[dict]) -> None:
    if not outcomes:
        return

    timestamp = datetime.datetime.now().isoformat()
    context.history.append(
        {
            "type": "self_heal",
            "timestamp": timestamp,
            "task": task,
            "outcomes": outcomes,
        }
    )
    context.history = context.history[-100:]

    memory_outcomes = context.memory.get("self_heal_outcomes")
    if not isinstance(memory_outcomes, list):
        memory_outcomes = []
    memory_outcomes.extend(outcomes)
    context.memory["self_heal_outcomes"] = memory_outcomes[-50:]

    known_fixes = context.memory.get("known_self_heal_fixes")
    if not isinstance(known_fixes, list):
        known_fixes = []

    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        failure_class = str(outcome.get("trigger_failure_class", "")).strip()
        known_fix = str(outcome.get("known_fix", "")).strip()
        tool_name = str(outcome.get("tool", "")).strip()
        if not failure_class or not known_fix or not tool_name:
            continue
        signature = f"{failure_class}|{tool_name}|{known_fix}"
        existing = next(
            (
                item
                for item in known_fixes
                if isinstance(item, dict) and str(item.get("signature", "")).strip() == signature
            ),
            None,
        )
        if existing is None:
            existing = {
                "signature": signature,
                "failure_class": failure_class,
                "tool": tool_name,
                "known_fix": known_fix,
                "success_count": 0,
                "failure_count": 0,
                "last_seen": timestamp,
            }
            known_fixes.append(existing)
        if bool(outcome.get("resolved")):
            existing["success_count"] = _safe_int(existing.get("success_count"), 0) + 1
        else:
            existing["failure_count"] = _safe_int(existing.get("failure_count"), 0) + 1
        existing["last_seen"] = timestamp
    context.memory["known_self_heal_fixes"] = known_fixes[-30:]

    lines = [f"## Self-heal outcomes - {timestamp}", f"Task: {task}"]
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        outcome_status = "resolved" if bool(outcome.get("resolved")) else "failed"
        last_error = str(outcome.get("last_error", "")).replace("\n", " ").strip()
        lines.append(
            (
                f"- Step {outcome.get('step')} ({outcome.get('tool')}): "
                f"class={outcome.get('trigger_failure_class') or 'unknown'}, "
                f"retries={outcome.get('retry_count', 0)}, outcome={outcome_status}, "
                f"fix={outcome.get('known_fix') or 'n/a'}, last_error={last_error or 'n/a'}"
            )
        )

    try:
        append_memory("\n".join(lines))
    except OSError:
        return


def _build_action_trace(steps: list[dict] | None, tool_results: list[dict] | None) -> list[dict]:
    if not steps:
        return []

    result_by_step: dict[int, dict[str, object]] = {}
    for item in tool_results or []:
        if not isinstance(item, dict):
            continue
        step_value = item.get("step")
        try:
            step_index = int(step_value)
        except (TypeError, ValueError):
            continue
        payload = item.get("result", {})
        status = "unknown"
        message = ""
        result_preview = ""
        result_debug: dict[str, object] = {}
        if isinstance(payload, dict):
            raw_status = str(payload.get("status", "")).strip().lower()
            if raw_status:
                status = raw_status
            message = str(payload.get("message", "")).strip()
            payload_result = payload.get("result")
            if payload_result is not None:
                result_preview = _truncate_text(
                    _normalize_whitespace(_safe_json_dumps(payload_result)),
                    240,
                )
            debug_payload = payload.get("debug")
            if isinstance(debug_payload, dict):
                result_debug = {
                    "raw_type": str(debug_payload.get("raw_type", "")).strip(),
                    "approx_chars": max(0, _safe_int(debug_payload.get("approx_chars"), 0)),
                    "summary_chars": max(0, _safe_int(debug_payload.get("summary_chars"), 0)),
                    "truncated": bool(debug_payload.get("truncated")),
                }
        result_by_step[step_index] = {
            "status": status,
            "message": message[:300],
            "result_preview": result_preview,
            "result_debug": result_debug,
            "policy": _sanitize_policy_metadata(item.get("policy")),
            "retry": _sanitize_retry_metadata(item.get("retry")),
        }

    action_trace: list[dict] = []
    for fallback_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        step_value = step.get("step", fallback_index)
        try:
            step_index = int(step_value)
        except (TypeError, ValueError):
            step_index = fallback_index

        tool_name = str(step.get("tool", "")).strip()
        args = step.get("args", {})
        if not isinstance(args, dict):
            args = {}
        reason = str(step.get("reason", "")).strip()
        requires_approval = bool(step.get("requires_approval")) or tool_requires_approval(tool_name)
        human_approved = bool(step.get("human_approved"))
        step_policy = _sanitize_policy_metadata(step.get("policy"))
        step_retry = _sanitize_retry_metadata(step.get("retry"))
        result_info = result_by_step.get(
            step_index,
            {
                "status": "unknown",
                "message": "",
                "result_preview": "",
                "result_debug": {},
                "policy": step_policy,
                "retry": step_retry,
            },
        )
        policy_info = _sanitize_policy_metadata(result_info.get("policy"))
        retry_info = result_info.get("retry")
        if not isinstance(retry_info, dict):
            retry_info = step_retry

        action_trace.append(
            {
                "step": step_index,
                "tool": tool_name,
                "args": args,
                "reason": reason,
                "status": result_info["status"],
                "message": result_info["message"],
                "result_preview": str(result_info.get("result_preview", "")),
                "result_debug": result_info.get("result_debug", {}),
                "requires_approval": requires_approval,
                "human_approved": human_approved,
                "access_level": policy_info["access_level"],
                "policy": policy_info,
                "retry_count": max(0, _safe_int(retry_info.get("retry_count"), 0)),
                "max_retries": max(0, _safe_int(retry_info.get("max_retries"), 0)),
                "retries_exhausted": bool(retry_info.get("retries_exhausted")),
                "failure_class": str(retry_info.get("trigger_failure_class", "")).strip(),
                "known_fix": str(retry_info.get("known_fix", "")).strip(),
                "retry_attempts": retry_info.get("attempts", []),
                "correction_context": retry_info.get("correction_context", {}),
                "learn_teach_affordance": retry_info.get("learn_teach_affordance", {}),
            }
        )

    return action_trace


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
    steps: list[dict] | None = None,
    tool_results: list[dict] | None = None,
    plan_retries: int = 0,
    self_heal_retries: int = 0,
    self_heal_outcomes: list[dict] | None = None,
    decomposition_used: bool = False,
    decomposed_steps: list[str] | None = None,
    step_routes: list[dict] | None = None,
    access_level_policy: str = "L1-L3",
) -> None:
    action_trace = _build_action_trace(steps=steps, tool_results=tool_results)
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task": task,
        "success": success,
        "steps_completed": steps_completed,
        "format_failures": format_failures,
        "retries": retries,
        "plan_retries": plan_retries,
        "self_heal_retries": self_heal_retries,
        "final_output": final_output[:300] if final_output else "",
        "error": error,
        "elapsed_seconds": elapsed_seconds,
        "router_decision": router_decision,
        "router_confidence": router_confidence,
        "model": model_used,
        "action_trace": action_trace,
        "tool_results": _build_tool_results_log(tool_results),
        "self_heal_outcomes": self_heal_outcomes or [],
        "decomposition_used": decomposition_used,
        "decomposed_steps": decomposed_steps or [],
        "step_routes": step_routes or [],
        "access_level_policy": access_level_policy,
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
    last_message = ""
    if tool_results:
        last_result = tool_results[-1].get("result", {})
        if isinstance(last_result, dict):
            last_message = str(last_result.get("message", ""))
    lines = [
        f"## Incomplete Task - {datetime.datetime.now().isoformat()}",
        f"**Task:** {task}",
        f"**Completed:** {len(tool_results)} steps",
        f"**Last output:** {last_message}",
        f"**Remaining steps:** {max(len(plan) - len(tool_results), 0)}",
    ]
    try:
        append_context_entry("\n".join(lines))
    except (OSError, SecureMemoryError):
        return


def _has_human_approval(task: str, args: dict) -> bool:
    approval_arg = args.get("human_approval")
    if isinstance(approval_arg, bool):
        return approval_arg
    if isinstance(approval_arg, str):
        if approval_arg.strip().lower() in {"true", "yes", "approved"}:
            return True

    text = task.lower()
    approval_phrases = [
        "i approve",
        "approved",
        "with approval",
        "you are approved",
        "go ahead and run",
        "confirm and run",
        "human approval",
    ]
    return any(phrase in text for phrase in approval_phrases)


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

    privileged_plan = _build_privileged_command_fastpath_plan(task=task, text=text)
    if privileged_plan is not None:
        return privileged_plan

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

    availability_plan = _build_app_availability_fastpath_plan(task=task, text=text)
    if availability_plan is not None:
        return availability_plan

    document_open_plan = _build_document_open_fastpath_plan(task=task, text=text)
    if document_open_plan is not None:
        return document_open_plan

    spotify_plan = _build_spotify_fastpath_plan(task=task, text=text)
    if spotify_plan is not None:
        return spotify_plan

    explorer_search_plan = _build_explorer_search_fastpath_plan(task=task, text=text)
    if explorer_search_plan is not None:
        return explorer_search_plan

    local_path_search_plan = _build_local_path_search_fastpath_plan(task=task, text=text)
    if local_path_search_plan is not None:
        return local_path_search_plan

    index_plan = _build_index_fastpath_plan(task=task, text=text)
    if index_plan is not None:
        return index_plan

    calculator_plan = _build_calculator_fastpath_plan(task=task)
    if calculator_plan is not None:
        return calculator_plan

    notepad_plan = _build_notepad_write_fastpath_plan(task=task, text=text)
    if notepad_plan is not None:
        return notepad_plan

    window_action_plan = _build_window_action_fastpath_plan(task=task, text=text)
    if window_action_plan is not None:
        return window_action_plan

    browser_profile_switch_plan = _build_browser_profile_switch_fastpath_plan(task=task, text=text)
    if browser_profile_switch_plan is not None:
        return browser_profile_switch_plan

    browser_stress_plan = _build_browser_stress_fastpath_plan(task=task, text=text)
    if browser_stress_plan is not None:
        return browser_stress_plan

    youtube_comment_plan = _build_youtube_comment_fastpath_plan(task=task, text=text)
    if youtube_comment_plan is not None:
        return youtube_comment_plan

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

    codegen_autofix_plan = _build_web_codegen_autofix_fastpath_plan(task=task, text=text)
    if codegen_autofix_plan is not None:
        return codegen_autofix_plan

    file_plan = _build_file_generation_fastpath_plan(task=task, text=text)
    if file_plan is not None:
        return file_plan

    return None


def _build_app_availability_fastpath_plan(task: str, text: str) -> list[dict] | None:
    availability_tokens = ["available", "installed", "launchable", "availability", "can run", "can launch"]
    if not any(token in text for token in availability_tokens):
        return None

    extension = _extract_handler_extension_for_query(text=text)
    if extension is not None:
        return [
            {
                "tool": "check_file_handler",
                "args": {"extension": extension},
                "reason": "Check whether this file extension has a launchable default Windows handler.",
            }
        ]

    app_name = _extract_app_name_for_availability_query(task=task, text=text)
    if app_name is None:
        return None
    return [
        {
            "tool": "check_app_availability",
            "args": {"app_name": app_name},
            "reason": "Check if requested desktop application is launchable on this PC.",
        }
    ]


def _extract_handler_extension_for_query(text: str) -> str | None:
    if "pdf" in text:
        return ".pdf"
    if "pptx" in text or re.search(r"\bppt\b", text):
        return ".pptx"
    if "powerpoint" in text and any(token in text for token in ["file", "files", "slide", "slides", "handler"]):
        return ".pptx"
    return None


def _extract_app_name_for_availability_query(task: str, text: str) -> str | None:
    if "spotify" in text:
        return "spotify"
    if "powerpoint" in text:
        return "powerpoint"

    direct_match = re.search(
        r"\b(?:is|check(?:\s+if)?|verify(?:\s+if)?|confirm(?:\s+if)?|whether)\s+"
        r"([a-z0-9][a-z0-9 \-]{1,50}?)"
        r"(?:\s+(?:app|application|software))?\s+"
        r"(?:available|installed|launchable)\b",
        task,
        flags=re.IGNORECASE,
    )
    if direct_match:
        candidate = _clean_browser_action_text(direct_match.group(1))
        return candidate if candidate else None

    trailing_match = re.search(
        r"\b([a-z0-9][a-z0-9 \-]{1,50})\s+(?:is\s+)?(?:available|installed|launchable)\b",
        task,
        flags=re.IGNORECASE,
    )
    if trailing_match:
        candidate = _clean_browser_action_text(trailing_match.group(1))
        return candidate if candidate else None
    return None


def _build_document_open_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if "open" not in text:
        return None

    extension = _extract_document_extension_hint(text=text)
    if extension is None:
        return None

    path = _extract_document_path(task=task, extension_hint=extension)
    if path:
        return [
            {
                "tool": "check_file_handler",
                "args": {"extension": extension},
                "reason": "Check whether this document extension has a launchable default handler.",
            },
            {
                "tool": "open_file_with_default_app",
                "args": {"path": path},
                "reason": "Open requested local document path with the default Windows app.",
            },
        ]

    query_hint = _extract_document_query(task=task, extension_hint=extension)
    args: dict[str, object] = {"extension": extension}
    if query_hint:
        args["query"] = query_hint

    return [
        {
            "tool": "open_existing_document",
            "args": args,
            "reason": "Find and open an existing local document for the requested type.",
        }
    ]


def _extract_document_extension_hint(text: str) -> str | None:
    pdf_match = re.search(r"\.pdf\b|\bpdf\b", text, flags=re.IGNORECASE)
    ppt_match = re.search(r"\.pptx?\b|\bpptx?\b", text, flags=re.IGNORECASE)
    if not ppt_match and "powerpoint" in text:
        if any(token in text for token in ["file", "files", "slide", "slides", "deck"]):
            ppt_match = re.search(r"\bpowerpoint\b", text, flags=re.IGNORECASE)
    if pdf_match and ppt_match:
        return ".pdf" if pdf_match.start() <= ppt_match.start() else ".pptx"
    if pdf_match:
        return ".pdf"
    if ppt_match:
        return ".pptx"
    return None


def _extract_document_path(task: str, extension_hint: str) -> str | None:
    allowed_extensions = {".pdf"} if extension_hint == ".pdf" else {".ppt", ".pptx"}

    def _matches(candidate: str) -> bool:
        suffix = os.path.splitext(candidate)[1].lower()
        return suffix in allowed_extensions

    quoted_matches = re.findall(r'"([^"]+)"|\'([^\']+)\'', task)
    for left, right in quoted_matches:
        candidate = (left or right).strip()
        if not candidate:
            continue
        if _matches(candidate):
            return candidate

    windows_path_match = re.search(
        r"([a-zA-Z]:\\[^\"'\r\n]+\.(?:pdf|ppt|pptx))",
        task,
        flags=re.IGNORECASE,
    )
    if windows_path_match:
        candidate = windows_path_match.group(1).strip().rstrip(".,;:!?")
        if _matches(candidate):
            return candidate

    generic_match = re.search(r"([^\s\"']+\.(?:pdf|ppt|pptx))", task, flags=re.IGNORECASE)
    if generic_match:
        candidate = generic_match.group(1).strip().rstrip(".,;:!?")
        if _matches(candidate):
            return candidate
    return None


def _extract_document_query(task: str, extension_hint: str) -> str | None:
    quoted_matches = re.findall(r'"([^"]+)"|\'([^\']+)\'', task)
    for left, right in quoted_matches:
        candidate = (left or right).strip()
        if not candidate:
            continue
        if os.path.splitext(candidate)[1].lower() in {".pdf", ".ppt", ".pptx"}:
            continue
        if len(candidate) >= 3:
            return candidate[:80]

    if extension_hint == ".pdf":
        marker_pattern = r"\bpdf\b.*\bfor\s+(.+)$"
    else:
        marker_pattern = r"\b(?:ppt|pptx|powerpoint)\b.*\bfor\s+(.+)$"
    marker_match = re.search(marker_pattern, task, flags=re.IGNORECASE)
    if marker_match:
        candidate = marker_match.group(1).strip()
        candidate = re.sub(
            r"\b(?:file|files|on|in|desktop|documents|downloads|folder|pc|computer)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" .,:;!?")
        if len(candidate) >= 3:
            return candidate[:80]
    return None


def _build_spotify_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if "spotify" not in text:
        return None
    if not any(token in text for token in ["open", "play", "search"]):
        return None

    query = _extract_spotify_query(task=task)
    args: dict[str, object] = {}
    if query:
        args["query"] = query
        reason = "Open Spotify and attempt search/play for requested track."
    else:
        reason = "Open Spotify with availability precheck and graceful fallback."
    return [{"tool": "spotify_play", "args": args, "reason": reason}]


def _extract_spotify_query(task: str) -> str | None:
    patterns = [
        r"\bopen\s+spotify\b(?:\s+and)?\s+play\s+(.+)$",
        r"\bplay\s+(.+?)\s+on\s+spotify\b",
        r"\bopen\s+spotify\b(?:\s+and)?\s+search(?:\s+for)?\s+(.+)$",
        r"\bspotify\b.*\bsearch(?:\s+for)?\s+(.+?)(?:\s+and\s+play|\s*$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, task, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = _clean_browser_action_text(match.group(1))
        if candidate:
            return candidate
    return None


def _extract_core_app_name(text: str) -> str | None:
    app_patterns = [
        (r"\bopen\s+notepad\b", "notepad"),
        (r"\bopen\s+calculator\b|\bopen\s+calc\b", "calculator"),
        (r"\bopen\s+file\s+explorer\b|\bopen\s+explorer\b", "explorer"),
        (r"\bopen\s+file\s+manager\b", "explorer"),
        (r"\bopen\s+settings\b", "settings"),
        (r"\bopen\s+power\s*point\b|\bopen\s+powerpoint\b", "powerpoint"),
    ]
    for pattern, app_name in app_patterns:
        if re.search(pattern, text):
            return app_name
    return None


def _build_privileged_command_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if "run command" not in text and "run shell command" not in text:
        return None
    match = re.search(r"\brun(?:\s+shell)?\s+command\s+(.+)$", task, flags=re.IGNORECASE)
    if not match:
        return None
    command = match.group(1).strip().strip("\"'")
    command = re.sub(
        r"\s*(?:,?\s*(?:and\s+)?(?:i\s+)?approve(?:d)?(?:\s+this)?(?:\s+command)?)\s*$",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip()
    if not command:
        return None
    args: dict[str, object] = {"command": command}
    if _has_human_approval(task=task, args={}):
        args["human_approval"] = True
    return [
        {
            "tool": "run_command",
            "args": args,
            "reason": _annotate_step_reason(
                step_text=task,
                route_path="local_privileged_fastpath",
                base_reason="Execute privileged command path with explicit approval checks.",
                tool_name="run_command",
            ),
        }
    ]


def _build_explorer_search_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if "explorer" not in text and "file explorer" not in text and "file manager" not in text:
        return None
    if not re.search(r"\b(find|fint|search|locate)\b", text):
        return None
    query = _extract_explorer_query(task=task, text=text)
    if query is None:
        return None
    return [
        {
            "tool": "search_in_explorer",
            "args": {"query": query, "folders_only": True},
            "reason": "Open File Explorer search for requested folder name.",
        }
    ]


def _extract_explorer_query(task: str, text: str) -> str | None:
    match = re.search(
        r"\b(?:find|fint|search|locate)\s+(?:for\s+)?(.+?)\s+(?:folder|directory)\b",
        task,
        flags=re.IGNORECASE,
    )
    if match:
        query = match.group(1).strip().strip("\"'")
        return query if query else None

    fallback = re.search(
        r"\b(?:find|fint|search|locate)\s+(?:for\s+)?(.+)$",
        task,
        flags=re.IGNORECASE,
    )
    if fallback:
        query = fallback.group(1).strip().strip(" .!?\"'")
        if query:
            return query
    return None


def _build_local_path_search_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if not re.search(r"\b(search|find|locate)\b", text):
        return None

    local_scope_hint = any(
        token in text
        for token in [
            "on my pc",
            "on my computer",
            "in my pc",
            "in my computer",
            "in this pc",
            "on this pc",
            "local file",
            "local folder",
            "file on pc",
            "folder on pc",
        ]
    )
    file_hint = any(token in text for token in ["file", "folder", "directory", "path"])
    if not local_scope_hint and not file_hint:
        return None

    query = _extract_local_path_search_query(task=task, text=text)
    if query is None:
        return None
    lowered_query = query.lower()
    if (
        any(token in lowered_query for token in ["youtube", "video", "browser", "website", "chatgpt", "google"])
        and "on my pc" not in text
        and "on this pc" not in text
        and "in my pc" not in text
        and "in this pc" not in text
    ):
        return None

    kind = "folder" if any(token in text for token in ["folder", "directory"]) else "all"
    open_first = any(token in text for token in ["open", "go to", "navigate"])
    return [
        {
            "tool": "search_local_paths",
            "args": {"query": query, "kind": kind, "open_first": open_first},
            "reason": "Direct local command to search files/folders on this PC.",
        }
    ]


def _extract_local_path_search_query(task: str, text: str) -> str | None:
    _ = text
    def _clean_local_query(raw: str) -> str:
        cleaned = _clean_browser_action_text(raw)
        cleaned = re.sub(r"\b(file|files|folder|folders|directory|directories)\b$", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    pattern = (
        r"\b(?:search|find|locate)\s+(?:for\s+)?(.+?)"
        r"(?:\s+(?:on|in)\s+(?:my|this)\s+(?:pc|computer)|\s*$)"
    )
    match = re.search(pattern, task, flags=re.IGNORECASE)
    if match:
        query = _clean_local_query(match.group(1))
        if query:
            return query

    trailing = re.search(r"\b(?:search|find|locate)\s+(?:for\s+)?(.+)$", task, flags=re.IGNORECASE)
    if trailing:
        query = _clean_local_query(trailing.group(1))
        if query:
            return query
    return None


def _build_index_fastpath_plan(task: str, text: str) -> list[dict] | None:
    normalized = text.strip()
    if normalized in {"/index-app", "index apps", "index app", "index applications"} or re.search(
        r"\bindex\s+(?:apps?|applications)\b",
        text,
    ):
        return [
            {
                "tool": "index_apps",
                "args": {},
                "reason": "Build local application index for reliable app-name resolution.",
            }
        ]

    if normalized in {"/index", "index files", "index file"} or re.search(
        r"\bindex\s+(?:files?|filesystem|this pc|my pc)\b",
        text,
    ):
        scope = "full" if any(token in text for token in ["full", "all", "this pc", "my pc"]) else "quick"
        return [
            {
                "tool": "index_files",
                "args": {"scope": scope},
                "reason": "Build local file index for faster PC file search.",
            }
        ]
    return None


def _build_notepad_write_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if "notepad" not in text:
        return None
    if "write" not in text and "type" not in text:
        return None

    content = _extract_notepad_text(task=task)
    if content is None:
        return None

    return [
        {
            "tool": "write_in_notepad",
            "args": {"text": content},
            "reason": "Direct local command to open Notepad and type requested text.",
        }
    ]


def _extract_notepad_text(task: str) -> str | None:
    quoted_match = re.search(r'"([^"]+)"|\'([^\']+)\'', task)
    if quoted_match:
        quoted_text = quoted_match.group(1) or quoted_match.group(2)
        if quoted_text and quoted_text.strip():
            return quoted_text.strip()

    patterns = [
        r"\b(?:write|type)\s+(.+?)\s+(?:in|into)\s+notepad\b",
        r"\bopen\s+notepad\b(?:\s+and)?\s+(?:write|type)\s+(.+)$",
        r"\bnotepad\b.*\b(?:write|type)\b\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, task, flags=re.IGNORECASE)
        if match:
            content = match.group(1).strip().strip(" .!?")
            if content:
                return content
    return None


def _build_window_action_fastpath_plan(task: str, text: str) -> list[dict] | None:
    read_match = re.search(
        r"\b(?:read|get|show)\s+(?:window|app)\s+(?:state|elements?)\s+(?:for|of|in)\s+(.+)$",
        task,
        flags=re.IGNORECASE,
    )
    if read_match:
        window_title = _clean_browser_action_text(read_match.group(1))
        if window_title:
            return [
                {
                    "tool": "get_window_state",
                    "args": {"window_title": window_title},
                    "reason": "Read interactive controls from target desktop window.",
                }
            ]

    click_in_match = re.search(
        r"\bclick(?:\s+on)?\s+(.+?)\s+(?:in|inside)\s+(.+)$",
        task,
        flags=re.IGNORECASE,
    )
    if click_in_match:
        element_name = _clean_browser_action_text(click_in_match.group(1))
        window_title = _clean_browser_action_text(click_in_match.group(2))
        if element_name and window_title:
            return [
                {
                    "tool": "get_window_state",
                    "args": {"window_title": window_title},
                    "reason": "Read window controls before interaction.",
                },
                {
                    "tool": "click_in_window",
                    "args": {"window_title": window_title, "element_name": element_name},
                    "reason": "Click requested control inside target application window.",
                },
            ]

    open_click_match = re.search(
        r"\bopen\s+([a-z0-9 \-]+?)\s+and\s+click(?:\s+on)?\s+(.+)$",
        task,
        flags=re.IGNORECASE,
    )
    if open_click_match:
        app_name = _clean_browser_action_text(open_click_match.group(1))
        element_name = _clean_browser_action_text(open_click_match.group(2))
        if app_name and element_name:
            return [
                {
                    "tool": "open_app",
                    "args": {"app_name": app_name},
                    "reason": "Open requested desktop application.",
                },
                {
                    "tool": "get_window_state",
                    "args": {"window_title": app_name},
                    "reason": "Read window controls before interaction.",
                },
                {
                    "tool": "click_in_window",
                    "args": {"window_title": app_name, "element_name": element_name},
                    "reason": "Click requested control inside opened application.",
                },
            ]

    return None


def _build_calculator_fastpath_plan(task: str) -> list[dict] | None:
    expression = _extract_math_expression(task=task)
    if expression is None:
        return None
    return [
        {
            "tool": "open_app",
            "args": {"app_name": "calculator"},
            "reason": "Direct local command to open Calculator for arithmetic expression.",
        },
        {
            "tool": "focus_window",
            "args": {"window_title": "Calculator"},
            "reason": "Bring Calculator to foreground before typing expression.",
        },
        {
            "tool": "type_text",
            "args": {"text": expression},
            "reason": "Type arithmetic expression into Calculator.",
        },
        {
            "tool": "press_key",
            "args": {"key": "enter"},
            "reason": "Evaluate expression in Calculator.",
        },
    ]


def _extract_txt_filename(task: str) -> str | None:
    quoted_matches = re.findall(r'"([^"]+)"|\'([^\']+)\'', task)
    for left, right in quoted_matches:
        candidate = (left or right).strip()
        if candidate.lower().endswith(".txt"):
            return Path(candidate).name

    match = re.search(r"\b([a-zA-Z0-9_\-]+\.txt)\b", task)
    if match:
        return match.group(1)
    return None


def _extract_youtube_pipeline_query(task: str, text: str) -> str | None:
    patterns = [
        r"\bsearch(?:\s+for)?\s+(.+?)\s+on\s+youtube\b",
        r"\bon\s+youtube\b.*\bsearch(?:\s+for)?\s+(.+?)(?:\s+and|\s*$)",
        r"\byoutube(?:\s+comment(?:s)?(?:\s+pipeline)?)?\s+(?:for|about)\s+(.+?)(?:\s+and|\s*$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, task, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = _clean_browser_action_text(match.group(1))
        candidate = re.sub(r"\b(?:comments?|pipeline|extract|save|export)\b.*$", "", candidate, flags=re.IGNORECASE)
        if candidate:
            return candidate

    quoted_match = re.search(r'"([^"]+)"|\'([^\']+)\'', task)
    if quoted_match:
        candidate = (quoted_match.group(1) or quoted_match.group(2) or "").strip()
        if candidate and ".txt" not in candidate.lower():
            return candidate

    trailing = re.search(r"\byoutube\b.*\bfor\s+(.+)$", task, flags=re.IGNORECASE)
    if trailing:
        candidate = _clean_browser_action_text(trailing.group(1))
        candidate = re.sub(
            r"\b(?:comments?|pipeline|save|export|desktop|notepad)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        if candidate:
            return candidate

    if "youtube" in text:
        return "latest technology video"
    return None


def _build_browser_stress_fastpath_plan(task: str, text: str) -> list[dict] | None:
    normalized = text.strip()
    explicit_aliases = {
        "/stress-browser-50-sites",
        "stress-browser-50-sites",
        "stress browser 50 sites",
    }
    has_alias = normalized in explicit_aliases
    has_50_site_hint = bool(re.search(r"\b(?:50|fifty)\s*[- ]?(?:site|sites)\b", text))
    has_stress_hint = (
        "stress" in text
        and any(token in text for token in ["site", "sites", "website", "websites"])
        and any(token in text for token in ["browser", "web", "navigate", "visit"])
    )
    if not (has_alias or has_50_site_hint or has_stress_hint):
        return None

    site_count = 50
    count_match = re.search(r"\b(\d{1,3})\s*[- ]?(?:site|sites)\b", text)
    if count_match:
        site_count = max(1, min(200, int(count_match.group(1))))

    retries = 1
    retry_match = re.search(r"\bretr(?:y|ies)\s*(?:count\s*)?(\d+)\b", text)
    if retry_match:
        retries = max(0, min(5, int(retry_match.group(1))))

    timeout_seconds = 12
    timeout_match = re.search(r"\btimeout\s*(\d{1,2})\s*(?:s|sec|seconds)?\b", text)
    if timeout_match:
        timeout_seconds = max(3, min(60, int(timeout_match.group(1))))

    args: dict[str, object] = {
        "site_count": site_count,
        "retries": retries,
        "timeout_seconds": timeout_seconds,
        "require_http_ok": True,
        "open_each_in_new_tab": True,
    }
    preferred_browser = _extract_preferred_browser(text=text)
    if preferred_browser is not None:
        args["browser"] = preferred_browser
    if "allow non-200" in text or "allow non 200" in text:
        args["require_http_ok"] = False
    if "same tab" in text or "single tab" in text or "one tab" in text:
        args["open_each_in_new_tab"] = False
    if "super fast" in text or "very fast" in text:
        args["wait_after_load_ms"] = 40
    if "dry run" in text or "--dry-run" in text:
        args["dry_run"] = True

    return [
        {
            "tool": "browser_stress_50_sites",
            "args": args,
            "reason": "Run deterministic high-volume browser stress workflow with per-site retry diagnostics.",
        }
    ]


def _build_youtube_comment_fastpath_plan(task: str, text: str) -> list[dict] | None:
    normalized = text.strip()
    explicit_aliases = {
        "/stress-youtube-comment-pipeline",
        "stress-youtube-comment-pipeline",
        "youtube comment pipeline",
    }
    has_alias = normalized in explicit_aliases
    has_youtube_comment_hint = (
        "youtube" in text
        and "comment" in text
        and any(token in text for token in ["search", "play", "pause", "extract", "save", "export", "pipeline"])
    )
    if not (has_alias or has_youtube_comment_hint):
        return None

    query = _extract_youtube_pipeline_query(task=task, text=text)
    if query is None:
        return None

    comment_count = 20
    comment_match = re.search(r"\b(\d{1,3})\s+comments?\b", text)
    if comment_match:
        comment_count = max(1, min(120, int(comment_match.group(1))))

    pause_after_seconds = 2
    pause_match = re.search(r"\bpause(?:\s+after)?\s+(\d{1,2})\s*(?:s|sec|seconds)?\b", text)
    if pause_match:
        pause_after_seconds = max(0, min(12, int(pause_match.group(1))))

    args: dict[str, object] = {
        "query": query,
        "comment_count": comment_count,
        "pause_after_seconds": pause_after_seconds,
    }
    preferred_browser = _extract_preferred_browser(text=text)
    if preferred_browser is not None:
        args["browser"] = preferred_browser

    output_filename = _extract_txt_filename(task=task)
    if output_filename:
        args["output_filename"] = output_filename
    if "notepad" in text:
        args["open_in_notepad"] = True
    if "dry run" in text or "--dry-run" in text:
        args["dry_run"] = True

    return [
        {
            "tool": "youtube_comment_pipeline",
            "args": args,
            "reason": "Run deterministic YouTube search/play/pause/comment extraction pipeline with Desktop export.",
        }
    ]


def _build_web_codegen_autofix_fastpath_plan(task: str, text: str) -> list[dict] | None:
    normalized = text.strip()
    explicit_aliases = {
        "/stress-web-codegen-autofix",
        "stress-web-codegen-autofix",
        "web codegen autofix",
    }
    has_alias = normalized in explicit_aliases

    write_tokens = ["write", "create", "generate", "make", "build"]
    code_tokens = ["python", ".py", "script", "code", "program"]
    has_codegen_hint = (
        any(token in text for token in write_tokens)
        and any(token in text for token in code_tokens)
    )
    chatgpt_codegen_hint = (
        "chatgpt" in text
        and any(token in text for token in ["code", "script", "python", "file", "copy", "run"])
    )
    coding_file_hint = "coding file" in text or "code file" in text
    if not has_alias and not has_codegen_hint:
        if not (chatgpt_codegen_hint or coding_file_hint):
            return None
    if not has_alias and not has_codegen_hint and not chatgpt_codegen_hint and not coding_file_hint:
        return None

    filename = _extract_python_filename(task=task, text=text) or "generated_script.py"

    max_fix_rounds = 2
    fix_match = re.search(r"\b(?:fix|retry|rerun)\s*(\d+)\s*(?:times|rounds|retries)?\b", text)
    if fix_match:
        max_fix_rounds = max(0, min(6, int(fix_match.group(1))))

    run_timeout_seconds = 20
    timeout_match = re.search(r"\b(?:run\s+)?timeout\s*(\d{1,3})\s*(?:s|sec|seconds)?\b", text)
    if timeout_match:
        run_timeout_seconds = max(5, min(120, int(timeout_match.group(1))))

    args: dict[str, object] = {
        "request": task.strip(),
        "filename": filename,
        "max_fix_rounds": max_fix_rounds,
        "run_timeout_seconds": run_timeout_seconds,
    }
    if "dry run" in text or "--dry-run" in text:
        args["dry_run"] = True

    return [
        {
            "tool": "web_codegen_autofix",
            "args": args,
            "reason": "Generate Python code via ChatGPT/configured assistant/free-AI path, execute it, and auto-fix bounded runtime failures.",
        }
    ]


def _extract_math_expression(task: str) -> str | None:
    expression_pattern = r"([-+]?\d+(?:\.\d+)?)\s*([+\-*/xX])\s*([-+]?\d+(?:\.\d+)?)"
    full_match = re.fullmatch(rf"\s*{expression_pattern}\s*\??\s*", task)
    if full_match:
        left, operator, right = full_match.groups()
    else:
        embedded = re.search(expression_pattern, task)
        if not embedded:
            return None
        left, operator, right = embedded.groups()

    normalized_operator = "*" if operator in {"x", "X"} else operator
    return f"{left}{normalized_operator}{right}"


def _build_browser_profile_switch_fastpath_plan(task: str, text: str) -> list[dict] | None:
    request = _extract_browser_profile_switch_request(task=task, text=text)
    if request is None:
        return None

    args: dict[str, object] = {"profile_mode": request["profile_mode"], "relaunch": True}
    if request.get("browser"):
        args["browser"] = request["browser"]

    return [
        {
            "tool": "browser_switch_profile",
            "args": args,
            "reason": "Switch browser profile mode and relaunch deterministic browser session.",
        }
    ]


def _extract_browser_profile_switch_request(task: str, text: str) -> dict[str, str | None] | None:
    if "profile" not in text:
        return None
    if not re.search(r"\b(?:switch|set|change)\b", text):
        return None

    direct_pattern = re.search(
        r"\b(?:switch|set|change)\s+"
        r"(?:(chrome|edge|firefox)\s+)?"
        r"(?:browser\s+)?profile(?:\s+mode)?\s+"
        r"(?:to|as)\s+(default|snapshot|automation)\b",
        task,
        flags=re.IGNORECASE,
    )
    if direct_pattern:
        browser = (
            direct_pattern.group(1).lower()
            if direct_pattern.group(1)
            else _extract_preferred_browser(text=text)
        )
        return {"browser": browser, "profile_mode": direct_pattern.group(2).lower()}

    reverse_pattern = re.search(
        r"\b(?:switch|set|change)\s+"
        r"(?:(chrome|edge|firefox)\s+)?"
        r"(?:browser\s+)?to\s+(default|snapshot|automation)\s+profile(?:\s+mode)?\b",
        task,
        flags=re.IGNORECASE,
    )
    if reverse_pattern:
        browser = (
            reverse_pattern.group(1).lower()
            if reverse_pattern.group(1)
            else _extract_preferred_browser(text=text)
        )
        return {"browser": browser, "profile_mode": reverse_pattern.group(2).lower()}

    mode_match = re.search(r"\b(default|snapshot|automation)\b", text, flags=re.IGNORECASE)
    if mode_match and any(token in text for token in ["browser", "chrome", "edge", "firefox"]):
        return {"browser": _extract_preferred_browser(text=text), "profile_mode": mode_match.group(1).lower()}
    return None


def _build_browser_fastpath_plan(task: str, text: str) -> list[dict] | None:
    if _looks_like_codegen_request(text=text):
        return None

    url = _extract_browser_url(task=task, text=text)
    preferred_browser = _extract_preferred_browser(text=text)
    actions = _extract_browser_actions(task=task, text=text)
    if not _should_route_browser_fastpath(text=text, url=url, preferred_browser=preferred_browser, actions=actions):
        return None

    should_preserve_current_page = bool(
        url is None
        and actions
        and "go to" in text
        and any(token in text for token in ["video", "result", "latest"])
        and not any(token in text for token in ["open browser", "open chrome", "open edge", "open firefox"])
    )

    plan: list[dict] = []
    if url is not None:
        plan.append(
            {
                "tool": "browser_navigate",
                "args": {"url": url, "browser": preferred_browser},
                "reason": "Open website in automation browser for interactive actions.",
            }
        )
    elif not actions or not should_preserve_current_page:
        plan.append(
            {
                "tool": "browser_navigate",
                "args": {"url": "https://www.google.com", "browser": preferred_browser},
                "reason": "Open website in automation browser for interactive actions.",
            }
        )
    if not actions:
        return plan

    for action in actions:
        if action["kind"] in {"search", "type"}:
            type_args: dict[str, object] = {"text": action["text"]}
            if action.get("element_name"):
                type_args["element_name"] = action["element_name"]
            if action.get("multiline"):
                type_args["multiline"] = True
                type_args["newline_mode"] = str(action.get("newline_mode", "shift_enter"))
            if action.get("submit"):
                type_args["submit"] = True
            plan.append(
                {
                    "tool": "browser_type",
                    "args": type_args,
                    "reason": "Type requested text into active browser page.",
                }
            )
            continue

        if action["kind"] == "click":
            click_args: dict[str, object] = {"element_name": action["element_name"]}
            if action.get("role"):
                click_args["role"] = action["role"]
            if action.get("occurrence"):
                click_args["occurrence"] = action["occurrence"]
            plan.append(
                {
                    "tool": "browser_click",
                    "args": click_args,
                    "reason": "Click requested browser element.",
                }
            )
            continue

        if action["kind"] == "press":
            plan.append(
                {
                    "tool": "browser_press_key",
                    "args": {"key": action["key"]},
                    "reason": "Press requested key in active browser page.",
                }
            )
            continue

        if action["kind"] == "state":
            plan.append(
                {
                    "tool": "browser_get_state",
                    "args": {},
                    "reason": "Read current browser page state before next action.",
                }
            )

    return plan


def _looks_like_codegen_request(text: str) -> bool:
    write_tokens = ["write", "create", "generate", "make", "build"]
    code_tokens = ["python", ".py", "script", "code", "program", "coding file", "code file"]
    if any(token in text for token in ["coding file", "code file"]):
        return True
    if any(token in text for token in write_tokens) and any(token in text for token in code_tokens):
        return True
    return False


def _should_route_browser_fastpath(
    text: str,
    url: str | None,
    preferred_browser: str | None,
    actions: list[dict],
) -> bool:
    direct_browser_tokens = [
        "open browser",
        "open chrome",
        "open edge",
        "open firefox",
        "go to ",
        "website",
    ]
    if any(token in text for token in direct_browser_tokens):
        return True

    action_verbs = re.search(
        r"\b(open|visit|navigate|browse|search|click|type|write|press)\b",
        text,
        flags=re.IGNORECASE,
    )
    if url is not None and action_verbs:
        return True
    if preferred_browser is not None and action_verbs:
        return True
    if actions and (url is not None or preferred_browser is not None):
        return True
    if actions and any(token in text for token in ["browser", "chrome", "edge", "firefox", "website", "youtube"]):
        return True
    return False


def _extract_browser_url(task: str, text: str) -> str | None:
    explicit_match = re.search(r"(https?://[^\s\"']+)", task, flags=re.IGNORECASE)
    if explicit_match:
        return explicit_match.group(1).rstrip(".,;:!?)")

    go_to_match = re.search(
        r"\bgo to\s+([a-z0-9\-\.]+\.[a-z]{2,}(?:/[^\s]*)?)",
        text,
        flags=re.IGNORECASE,
    )
    if go_to_match:
        return _normalize_browser_url_candidate(go_to_match.group(1))

    domain_matches = re.findall(
        r"\b([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+(?:/[^\s\"']*)?)",
        text,
        flags=re.IGNORECASE,
    )
    for candidate in domain_matches:
        normalized = _normalize_browser_url_candidate(candidate)
        if normalized is not None:
            return normalized

    if "youtube" in text:
        return "https://www.youtube.com"
    if "google" in text:
        return "https://www.google.com"
    known_sites = {
        "chatgpt": "https://chatgpt.com",
        "github": "https://github.com",
        "gitlab": "https://gitlab.com",
        "twitter": "https://x.com",
        "linkedin": "https://www.linkedin.com",
        "gmail": "https://mail.google.com",
        "stackoverflow": "https://stackoverflow.com",
        "youtube": "https://www.youtube.com",
        "reddit": "https://www.reddit.com",
    }
    for keyword, url in known_sites.items():
        if re.search(rf"\b{re.escape(keyword.strip())}\b", text):
            return url
    return None


def _normalize_browser_url_candidate(candidate: str) -> str | None:
    cleaned = candidate.strip().strip("\"'").rstrip(".,;:!?)")
    if not cleaned:
        return None

    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned

    tld = cleaned.rsplit(".", 1)[-1].lower()
    if tld in {"exe", "msi", "bat", "cmd", "ps1", "lnk"}:
        return None
    return f"https://{cleaned}"


def _extract_preferred_browser(text: str) -> str | None:
    browser_map = {
        "chrome": "chrome",
        "edge": "edge",
        "firefox": "firefox",
    }
    for token, executable in browser_map.items():
        if re.search(rf"\b{token}\b", text):
            return executable
    return None


_BROWSER_SUBMIT_REGEX = re.compile(
    r"\b(?:send|submit)\b|\b(?:press|hit)\s+enter\b",
    flags=re.IGNORECASE,
)
_BROWSER_MULTILINE_REGEX = re.compile(
    r"\b(?:new\s*line|newline|line\s*break|next\s*line|multiline|multi[-\s]?line)\b",
    flags=re.IGNORECASE,
)
_BROWSER_NEWLINE_TOKEN_REGEX = re.compile(
    r"\b(?:new\s*line|newline|line\s*break|next\s*line)\b",
    flags=re.IGNORECASE,
)


def _browser_submit_requested(text: str, include_search: bool = False) -> bool:
    source = str(text or "")
    if _BROWSER_SUBMIT_REGEX.search(source):
        return True
    if include_search and re.search(r"\bsearch(?:\s+for)?\b", source, flags=re.IGNORECASE):
        return True
    return False


def _browser_multiline_requested(text: str) -> bool:
    return _BROWSER_MULTILINE_REGEX.search(str(text or "")) is not None


def _normalize_browser_multiline_text(value: str) -> str:
    cleaned = str(value or "").strip().strip("\"'").strip()
    cleaned = cleaned.rstrip(".,;:!?")
    if not cleaned:
        return ""
    converted = _BROWSER_NEWLINE_TOKEN_REGEX.sub("\n", cleaned)
    converted = re.sub(r"[ \t]*\n[ \t]*", "\n", converted)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in converted.splitlines()]
    return "\n".join(lines).strip()


def _extract_browser_actions(task: str, text: str) -> list[dict]:
    actions: list[dict] = []
    submit_requested = _browser_submit_requested(task)

    search_match = re.search(
        r"\bsearch(?:\s+for)?\s+(.+?)(?=\s+(?:and|then)\s+(?:play|click|open|press|type|write)\b|\s*$)",
        task,
        flags=re.IGNORECASE,
    )
    if search_match:
        query = _normalize_browser_multiline_text(_clean_browser_action_text(search_match.group(1)))
        if query:
            search_action: dict[str, object] = {
                "kind": "search",
                "text": query,
                "element_name": "search",
                "submit": _browser_submit_requested(task, include_search=True),
            }
            if _browser_multiline_requested(task) or "\n" in query:
                search_action["multiline"] = True
                search_action["newline_mode"] = "shift_enter"
            actions.append(
                search_action
            )
    elif re.search(r"\bgo to\s+.+\b(?:video|result)\b", task, flags=re.IGNORECASE):
        implicit_query = re.sub(r"^\s*go to\s+", "", task, flags=re.IGNORECASE).strip()
        implicit_query = _normalize_browser_multiline_text(_clean_browser_action_text(implicit_query))
        if implicit_query:
            actions.append(
                {
                    "kind": "search",
                    "text": implicit_query,
                    "element_name": "search",
                    "submit": True,
                }
            )

    type_or_write_match = re.search(
        r"\b(?:type|write|paste)\s+(.+?)(?=\s+(?:and|then)\s+(?:(?:press|hit)\s+enter|send|submit|click|play)\b|\s*$)",
        task,
        flags=re.IGNORECASE,
    )
    if type_or_write_match:
        typed_text = _normalize_browser_multiline_text(_clean_browser_action_text(type_or_write_match.group(1)))
        if typed_text:
            type_action: dict[str, object] = {
                "kind": "type",
                "text": typed_text,
                "submit": submit_requested,
            }
            if _browser_multiline_requested(task) or "\n" in typed_text:
                type_action["multiline"] = True
                type_action["newline_mode"] = "shift_enter"
            actions.append(type_action)

    play_match = re.search(
        r"\bplay\s+(?:the\s+)?(?:(first|second|third|\d+(?:st|nd|rd|th)?)\s+)?(?:video|result|item)\b",
        task,
        flags=re.IGNORECASE,
    )
    if play_match:
        occurrence = _ordinal_token_to_int(play_match.group(1))
        actions.append(
            {
                "kind": "click",
                "element_name": "video",
                "role": "link",
                "occurrence": occurrence,
            }
        )

    click_match = re.search(
        r"\bclick(?:\s+on)?\s+(.+?)(?=\s+(?:and|then)\s+(?:press|type|write|search|click|play)\b|\s*$)",
        task,
        flags=re.IGNORECASE,
    )
    if click_match:
        click_text = _clean_browser_action_text(click_match.group(1))
        if click_text:
            actions.append({"kind": "click", "element_name": click_text})

    press_match = re.search(
        r"\bpress\s+(enter|tab|escape|esc|backspace|space)\b",
        text,
        flags=re.IGNORECASE,
    )
    if press_match:
        key_map = {"esc": "Escape", "escape": "Escape", "space": "Space"}
        raw_key = press_match.group(1).lower()
        actions.append({"kind": "press", "key": key_map.get(raw_key, raw_key.capitalize())})

    if not actions and re.search(r"\b(read|scan|get)\b.*\b(page|elements|state)\b", text):
        actions.append({"kind": "state"})

    return actions


def _ordinal_token_to_int(token: str | None) -> int:
    if token is None:
        return 1
    normalized = token.strip().lower()
    if not normalized:
        return 1
    mapping = {"first": 1, "second": 2, "third": 3}
    if normalized in mapping:
        return mapping[normalized]
    match = re.search(r"\d+", normalized)
    if match:
        return max(1, int(match.group(0)))
    return 1


def _clean_browser_action_text(value: str) -> str:
    cleaned = value.strip().strip("\"'").strip()
    return cleaned.rstrip(".,;:!?")


def _build_file_generation_fastpath_plan(task: str, text: str) -> list[dict] | None:
    write_tokens = ["write", "create", "generate", "make"]
    if not any(token in text for token in write_tokens):
        return None
    if ".py" not in text and "python file" not in text and "python script" not in text:
        return None

    filename = _extract_python_filename(task=task, text=text)
    if filename is None:
        filename = "generated_script.py"
    return [
        {
            "tool": "web_codegen_autofix",
            "args": {"request": task.strip(), "filename": filename},
            "reason": "Generate Python code via configured assistant path and auto-fix runtime failures before success.",
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
