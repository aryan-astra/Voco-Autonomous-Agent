"""
VOCO evaluation and benchmark runner.

Run:
  python eval.py                   # legacy 20-prompt evaluation suite
  python eval.py suite             # same as above
  python eval.py benchmark         # benchmark scenarios + regression gate
  python eval.py export-learning-data  # curated HISTORY.jsonl -> SFT JSONL
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
from pathlib import Path
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from constants import HISTORY_FILE
from context import AgentContext
from orchestrator import run as orchestrator_run
from startup_check import run_startup_check
from tools import dispatch_tool


TEST_PROMPTS = [
    {"prompt": "List all files in the workspace folder", "category": "easy", "expected_path": "llm_task"},
    {"prompt": "Take a screenshot", "category": "easy", "expected_path": "os_action"},
    {"prompt": "What apps are currently running on my computer?", "category": "easy", "expected_path": "os_action"},
    {"prompt": "Open Notepad", "category": "easy", "expected_path": "os_action"},
    {"prompt": "Mute the audio", "category": "easy", "expected_path": "os_action"},
    {"prompt": "Open Chrome and go to YouTube", "category": "medium", "expected_path": "os_action"},
    {
        "prompt": "Search for 'MKBHD latest video' on YouTube and click the first result",
        "category": "medium",
        "expected_path": "os_action",
    },
    {
        "prompt": "Open Notepad, type 'Hello from VOCO', and take a screenshot",
        "category": "medium",
        "expected_path": "os_action",
    },
    {
        "prompt": "Search Google for 'Python tutorials 2026' and show me the first 5 results",
        "category": "medium",
        "expected_path": "os_action",
    },
    {
        "prompt": "Create a file called test.txt in the workspace with the content 'VOCO test'",
        "category": "medium",
        "expected_path": "llm_task",
    },
    {"prompt": "Open config.py and tell me the model name", "category": "router_stress", "expected_path": "llm_task"},
    {
        "prompt": "Open the browser and also check my workspace files",
        "category": "router_stress",
        "expected_path": "os_action",
    },
    {
        "prompt": "Show me what's in my workspace and also search YouTube for coding tutorials",
        "category": "router_stress",
        "expected_path": "os_action",
    },
    {"prompt": "Find all Python files in workspace", "category": "router_stress", "expected_path": "llm_task"},
    {"prompt": "Run the main.py file", "category": "router_stress", "expected_path": "llm_task"},
    {"prompt": "opn chrome and serch for mkbhd", "category": "messy", "expected_path": "os_action"},
    {"prompt": "check the latst vid of mkbhd on youtube", "category": "messy", "expected_path": "os_action"},
    {
        "prompt": "open files and browser and also like maybe search something",
        "category": "messy",
        "expected_path": "os_action",
    },
    {"prompt": "set volume", "category": "messy", "expected_path": "os_action"},
    {"prompt": "i need to see youtube", "category": "messy", "expected_path": "os_action"},
]

FAST_EVAL_PROMPTS = [
    {"prompt": "mute audio", "category": "fast_path", "expected_path": "os_action"},
    {"prompt": "what apps are running", "category": "fast_path", "expected_path": "os_action"},
    {"prompt": "open notepad", "category": "fast_path", "expected_path": "os_action"},
    {"prompt": "list workspace files", "category": "llm_simple", "expected_path": "llm_task"},
    {"prompt": "open chrome", "category": "llm_simple", "expected_path": "os_action"},
    {"prompt": "search youtube for lofi", "category": "llm_multi", "expected_path": "os_action"},
    {"prompt": "find files in sample-search-space", "category": "file_search", "expected_path": "llm_task"},
    {"prompt": "take a screenshot", "category": "fast_path", "expected_path": "os_action"},
    {"prompt": "opn chrome", "category": "typo_test", "expected_path": "os_action"},
    {"prompt": "search for python file", "category": "file_search", "expected_path": "llm_task"},
]


@dataclass(frozen=True)
class BenchmarkStep:
    kind: str
    task: str | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    category: str
    description: str
    steps: tuple[BenchmarkStep, ...]
    required_tools: tuple[str, ...] = ()
    completion_indicators: tuple[str, ...] = ()
    completion_output_markers: tuple[str, ...] = ()
    completion_artifact_paths: tuple[str, ...] = ()
    enforce_no_duplicate_open_app: bool = False
    allow_duplicate_open_app_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class StressCapabilityBaselineScenario:
    scenario_id: str
    requested_flow: str
    priority: str
    support_level: str
    verification_prompt: str
    expected_tools: tuple[str, ...]
    key_gaps: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def task_step(task: str) -> BenchmarkStep:
    return BenchmarkStep(kind="task", task=task)


def tool_step(tool: str, args: dict[str, Any] | None = None) -> BenchmarkStep:
    return BenchmarkStep(kind="tool", tool=tool, args=dict(args or {}))


BENCHMARK_SCENARIOS: tuple[BenchmarkScenario, ...] = (
    BenchmarkScenario(
        scenario_id="browser-search-submit",
        category="browser",
        description="Navigate to DuckDuckGo and submit a search query.",
        steps=(task_step("open browser and go to duckduckgo.com and search for github copilot"),),
        required_tools=("browser_navigate", "browser_type"),
    ),
    BenchmarkScenario(
        scenario_id="browser-youtube-play-first",
        category="browser",
        description="Search YouTube and click the first video result.",
        steps=(task_step("open youtube and search mkbhd and play the 1st video"),),
        required_tools=("browser_navigate", "browser_type", "browser_click"),
    ),
    BenchmarkScenario(
        scenario_id="tool-first-decompose-browser-notepad",
        category="browser",
        description=(
            "Validate decomposition-first routing on a multi-action prompt "
            "(browser search + notepad write) without misrouting to local file search."
        ),
        steps=(
            task_step(
                'open chrome and go to youtube and search for mkbhd and then open notepad and write "routing test done" in notepad'
            ),
        ),
        required_tools=("browser_navigate", "browser_type", "write_in_notepad"),
    ),
    BenchmarkScenario(
        scenario_id="desktop-notepad-write",
        category="desktop",
        description="Open Notepad and type benchmark text.",
        steps=(task_step('open notepad and write "VOCO benchmark ping" in notepad'),),
        required_tools=("write_in_notepad",),
    ),
    BenchmarkScenario(
        scenario_id="desktop-notepad-click-file",
        category="desktop",
        description="Open Notepad and click the File menu via desktop UI controls.",
        steps=(task_step("open notepad and click file"),),
        required_tools=("open_app", "get_window_state", "click_in_window"),
    ),
    BenchmarkScenario(
        scenario_id="local-file-index-search",
        category="local_index",
        description="Build file index and query indexed file metadata.",
        steps=(
            tool_step("index_files", {"scope": "quick", "max_files": 8000}),
            tool_step("search_file", {"query": "Windows", "limit": 5, "open_first": False, "kind": "all"}),
        ),
        required_tools=("index_files", "search_file"),
        completion_output_markers=("indexed", "matches for"),
        completion_artifact_paths=("memory\\file_index.db",),
    ),
    BenchmarkScenario(
        scenario_id="local-app-index-open",
        category="local_index",
        description="Build app index then open Notepad using indexed app resolution.",
        steps=(tool_step("index_apps"), task_step("open notepad")),
        required_tools=("index_apps", "open_app"),
    ),
    BenchmarkScenario(
        scenario_id="complex-browser-search-click-extract",
        category="complex",
        description=(
            "Execute a decomposed browser workflow: navigate, search, click the first result, "
            "then capture browser state for extraction-style verification."
        ),
        steps=(
            tool_step("browser_navigate", {"url": "https://duckduckgo.com"}),
            tool_step("browser_type", {"text": "github copilot cli documentation"}),
            tool_step("browser_press_key", {"key": "Enter"}),
            tool_step("browser_click", {"element_name": "result", "occurrence": 1}),
            tool_step("browser_get_state", {"max_elements": 40}),
        ),
        required_tools=("browser_navigate", "browser_type", "browser_press_key", "browser_click", "browser_get_state"),
        completion_indicators=("browser_click", "browser_get_state"),
    ),
    BenchmarkScenario(
        scenario_id="complex-desktop-browser-handoff",
        category="complex",
        description="Mix desktop and browser actions: open Notepad, write text, then run browser search and capture state.",
        steps=(
            tool_step("open_app", {"app_name": "notepad"}),
            tool_step("write_in_notepad", {"text": "VOCO complex desktop-browser checkpoint"}),
            tool_step("browser_navigate", {"url": "https://duckduckgo.com"}),
            tool_step("browser_type", {"text": "windows notepad shortcuts"}),
            tool_step("browser_press_key", {"key": "Enter"}),
            tool_step("browser_get_state", {"max_elements": 35}),
        ),
        required_tools=("open_app", "write_in_notepad", "browser_navigate", "browser_type", "browser_press_key", "browser_get_state"),
        completion_indicators=("write_in_notepad", "browser_get_state"),
        enforce_no_duplicate_open_app=True,
    ),
    BenchmarkScenario(
        scenario_id="complex-index-assisted-retrieval",
        category="complex",
        description="Build file/app indexes, retrieve indexed file metadata, and open a desktop app via indexed resolution.",
        steps=(
            tool_step("index_files", {"scope": "quick", "max_files": 12000}),
            tool_step("search_file", {"query": "eval.py", "limit": 5, "open_first": False, "kind": "all"}),
            tool_step("index_apps"),
            tool_step("open_app", {"app_name": "notepad"}),
        ),
        required_tools=("index_files", "search_file", "index_apps", "open_app"),
        completion_indicators=("search_file", "open_app"),
        completion_artifact_paths=("memory\\file_index.db", "memory\\app_index.db"),
        enforce_no_duplicate_open_app=True,
    ),
    BenchmarkScenario(
        scenario_id="stress-browser-50-sites",
        category="stress",
        description="Run deterministic 50-site browser stress workflow with per-site telemetry (dry-run).",
        steps=(tool_step("browser_stress_50_sites", {"site_count": 50, "retries": 1, "dry_run": True}),),
        required_tools=("browser_stress_50_sites",),
    ),
    BenchmarkScenario(
        scenario_id="stress-youtube-comment-pipeline",
        category="stress",
        description="Run YouTube search/play/pause/comment-export pipeline in deterministic dry-run mode.",
        steps=(
            tool_step(
                "youtube_comment_pipeline",
                {
                    "query": "mkbhd latest video",
                    "comment_count": 10,
                    "output_filename": "benchmark_youtube_comments.txt",
                    "dry_run": True,
                },
            ),
        ),
        required_tools=("youtube_comment_pipeline",),
    ),
    BenchmarkScenario(
        scenario_id="stress-web-codegen-autofix",
        category="stress",
        description="Run web-codegen execute/autofix loop in deterministic dry-run mode.",
        steps=(
            tool_step(
                "web_codegen_autofix",
                {
                    "request": "Create a python script that prints hello world",
                    "filename": "benchmark_codegen_autofix.py",
                    "max_fix_rounds": 2,
                    "dry_run": True,
                },
            ),
        ),
        required_tools=("web_codegen_autofix",),
    ),
)

BENCHMARK_CATEGORIES: tuple[str, ...] = tuple(sorted({scenario.category for scenario in BENCHMARK_SCENARIOS}))

STRESS_CAPABILITY_BASELINE: tuple[StressCapabilityBaselineScenario, ...] = (
    StressCapabilityBaselineScenario(
        scenario_id="stress-chrome-profile-switching",
        requested_flow="Chrome profile switching",
        priority="P0",
        support_level="partial",
        verification_prompt="Open Chrome with the Work profile, then switch to Personal profile and confirm active profile.",
        expected_tools=("browser_navigate", "browser_get_state"),
        key_gaps=(
            "No tool argument for explicit Chrome profile name selection.",
            "No validation primitive for active browser profile identity.",
        ),
        evidence_refs=(
            "tools.py:_launch_playwright_context",
            "tools.py:_get_default_profile_config",
            "orchestrator.py:_extract_preferred_browser",
        ),
    ),
    StressCapabilityBaselineScenario(
        scenario_id="stress-50-site-batch-browse-search",
        requested_flow="50-site browsing/search batch",
        priority="P0",
        support_level="implemented",
        verification_prompt="Search and visit 50 websites, capture per-site outcomes, and summarize failures.",
        expected_tools=("browser_stress_50_sites",),
        key_gaps=(
            "External site outages can still fail individual targets and should be surfaced as explicit failures.",
            "Runtime performance varies with network conditions and browser startup cost.",
        ),
        evidence_refs=(
            "tools.py:browser_stress_50_sites",
            "orchestrator.py:_build_browser_stress_fastpath_plan",
            "eval.py:BENCHMARK_SCENARIOS[stress-browser-50-sites]",
        ),
    ),
    StressCapabilityBaselineScenario(
        scenario_id="stress-youtube-search-play-comment-export",
        requested_flow="YouTube search/play/pause/comments/export",
        priority="P0",
        support_level="implemented",
        verification_prompt="Search YouTube, play and pause a video, read comments, then save comments to Desktop.",
        expected_tools=(
            "youtube_comment_pipeline",
            "save_text_to_desktop_file",
        ),
        key_gaps=(
            "Age-restricted/private videos still require manual sign-in before comment extraction can proceed.",
            "Comment availability depends on YouTube page rendering and per-video settings.",
        ),
        evidence_refs=(
            "tools.py:youtube_comment_pipeline",
            "tools.py:_collect_youtube_comments",
            "tools.py:save_text_to_desktop_file",
            "orchestrator.py:_build_youtube_comment_fastpath_plan",
        ),
    ),
    StressCapabilityBaselineScenario(
        scenario_id="stress-codegen-run-self-fix-loop",
        requested_flow="Web-assistant codegen -> .py run -> self-fix loop",
        priority="P0",
        support_level="implemented",
        verification_prompt="Generate Python file from web instructions, execute it, auto-fix failures, and rerun until pass.",
        expected_tools=("web_codegen_autofix",),
        key_gaps=(
            "Requires either configured web-assistant command or healthy free-AI (Ollama) path.",
            "Complex scripts can still exceed bounded fix rounds and return explicit unresolved diagnostics.",
        ),
        evidence_refs=(
            "tools.py:web_codegen_autofix",
            "tools.py:_request_codegen_candidate",
            "orchestrator.py:_build_web_codegen_autofix_fastpath_plan",
            "eval.py:BENCHMARK_SCENARIOS[stress-web-codegen-autofix]",
        ),
    ),
    StressCapabilityBaselineScenario(
        scenario_id="stress-file-app-media-orchestration",
        requested_flow="File/app/media orchestration with app availability checks",
        priority="P1",
        support_level="improved",
        verification_prompt="Open PDF and PPT files, start Spotify playback, and verify required apps are available before actions.",
        expected_tools=(
            "check_app_availability",
            "check_file_handler",
            "open_file_with_default_app",
            "open_extension_handler",
            "spotify_play",
        ),
        key_gaps=(
            "Spotify automation is best-effort UI interaction; playback state verification is not deterministic.",
        ),
        evidence_refs=(
            "tools.py:check_app_availability",
            "tools.py:check_file_handler",
            "tools.py:open_file_with_default_app",
            "tools.py:open_extension_handler",
            "tools.py:spotify_play",
            "orchestrator.py:_build_document_open_fastpath_plan",
            "orchestrator.py:_build_spotify_fastpath_plan",
            "tools.py:open_app",
        ),
    ),
    StressCapabilityBaselineScenario(
        scenario_id="stress-self-heal-memory-sensitive-handling",
        requested_flow="Self-heal + memory/profile updates with sensitive data handling",
        priority="P0",
        support_level="partial",
        verification_prompt="Recover from failed plan execution, update profile/memory safely, and avoid storing sensitive values.",
        expected_tools=("update_user_profile", "report_failure"),
        key_gaps=(
            "Recovery loop is limited to format correction and step-limit logging, not automatic remediation planning.",
            "No PII/sensitive-data detector or redaction before vault/history persistence.",
        ),
        evidence_refs=(
            "orchestrator.py:_log_format_failure",
            "orchestrator.py:_write_incomplete_state",
            "memory.py:append_event",
            "tools.py:update_user_profile",
        ),
    ),
)

SFT_SYSTEM_PROMPT = (
    "You are VOCO's action planner for Windows automation. "
    "Return only a JSON array where each item has tool, args, and reason."
)

UNSAFE_DATA_TOOLS = {
    "run_shell_command",
    "run_command",
    "disable_usb_device",
    "add_firewall_rule",
}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "approved", "1", "y"}
    return False


def _task_has_approval_phrase(task: str) -> bool:
    text = task.lower()
    approval_phrases = (
        "i approve",
        "approved",
        "with approval",
        "you are approved",
        "go ahead and run",
        "confirm and run",
        "human approval",
    )
    return any(phrase in text for phrase in approval_phrases)


def _normalize_tool_result_map(raw_tool_results: Any) -> dict[int, dict[str, str]]:
    if not isinstance(raw_tool_results, list):
        return {}
    tool_result_map: dict[int, dict[str, str]] = {}
    for item in raw_tool_results:
        if not isinstance(item, dict):
            continue
        try:
            step_index = int(item.get("step"))
        except (TypeError, ValueError):
            continue
        payload = item.get("result", {})
        status = ""
        message = ""
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).strip().lower()
            message = str(payload.get("message", "")).strip()
        tool_result_map[step_index] = {"status": status, "message": message}
    return tool_result_map


def _extract_action_trace(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_trace = record.get("action_trace")
    if not isinstance(raw_trace, list):
        raw_trace = record.get("steps")
    if not isinstance(raw_trace, list) or not raw_trace:
        return None

    tool_result_map = _normalize_tool_result_map(record.get("tool_results"))
    task_text = str(record.get("task", ""))
    normalized_trace: list[dict[str, Any]] = []

    for fallback_index, raw_step in enumerate(raw_trace, start=1):
        if not isinstance(raw_step, dict):
            return None

        tool_name = str(raw_step.get("tool", "")).strip()
        args = raw_step.get("args", {})
        if not tool_name or not isinstance(args, dict):
            return None

        try:
            step_index = int(raw_step.get("step", fallback_index))
        except (TypeError, ValueError):
            step_index = fallback_index

        reason = str(raw_step.get("reason", "")).strip()
        status = str(raw_step.get("status", "")).strip().lower()
        message = str(raw_step.get("message", "")).strip()
        if not status and step_index in tool_result_map:
            status = tool_result_map[step_index]["status"]
        if not message and step_index in tool_result_map:
            message = tool_result_map[step_index]["message"]
        if not status:
            status = "unknown"

        requires_approval = bool(raw_step.get("requires_approval")) or tool_name in UNSAFE_DATA_TOOLS
        human_approved = _is_truthy(raw_step.get("human_approved")) or _is_truthy(args.get("human_approval"))
        if not human_approved and requires_approval:
            human_approved = _task_has_approval_phrase(task_text)

        normalized_trace.append(
            {
                "step": step_index,
                "tool": tool_name,
                "args": args,
                "reason": reason,
                "status": status,
                "message": message[:300],
                "requires_approval": requires_approval,
                "human_approved": human_approved,
            }
        )

    return normalized_trace


def export_learning_data(
    *,
    history_path: str = HISTORY_FILE,
    output_path: str = str(Path("memory") / "vault" / "sft_command_traces.jsonl"),
    min_steps: int = 1,
    max_records: int | None = None,
) -> dict[str, Any]:
    history_file = Path(history_path)
    if not history_file.exists():
        raise ValueError(f"History file not found: {history_file}")
    if min_steps < 1:
        raise ValueError("min_steps must be >= 1.")
    if max_records is not None and max_records < 1:
        raise ValueError("max_records must be >= 1 when provided.")

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "history_file": str(history_file.resolve()),
        "output_file": str(output_file.resolve()),
        "records_seen": 0,
        "exported": 0,
        "excluded_failures": 0,
        "excluded_malformed": 0,
        "excluded_unsafe": 0,
        "excluded_too_short": 0,
    }

    with history_file.open("r", encoding="utf-8") as source, output_file.open("w", encoding="utf-8") as target:
        for raw_line in source:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            stats["records_seen"] += 1
            if max_records is not None and stats["records_seen"] > max_records:
                break

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["excluded_malformed"] += 1
                continue
            if not isinstance(record, dict):
                stats["excluded_malformed"] += 1
                continue

            task = str(record.get("task", "")).strip()
            if not task:
                stats["excluded_malformed"] += 1
                continue
            if not bool(record.get("success")):
                stats["excluded_failures"] += 1
                continue

            action_trace = _extract_action_trace(record)
            if not action_trace:
                stats["excluded_malformed"] += 1
                continue
            if len(action_trace) < min_steps:
                stats["excluded_too_short"] += 1
                continue

            if any(step.get("status") != "success" for step in action_trace):
                stats["excluded_failures"] += 1
                continue

            if any(
                step["tool"] in UNSAFE_DATA_TOOLS and not bool(step.get("human_approved"))
                for step in action_trace
            ):
                stats["excluded_unsafe"] += 1
                continue

            plan = [
                {
                    "tool": step["tool"],
                    "args": step["args"],
                    "reason": step["reason"],
                }
                for step in action_trace
            ]

            sample = {
                "messages": [
                    {"role": "system", "content": SFT_SYSTEM_PROMPT},
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                ],
                "action_trace": action_trace,
                "metadata": {
                    "timestamp": record.get("timestamp"),
                    "router_decision": record.get("router_decision"),
                    "router_confidence": record.get("router_confidence"),
                    "model": record.get("model"),
                    "elapsed_seconds": record.get("elapsed_seconds"),
                    "final_output": str(record.get("final_output", ""))[:300],
                },
            }
            target.write(json.dumps(sample, ensure_ascii=False) + "\n")
            stats["exported"] += 1

    print("=" * 60)
    print("LEARNING DATA EXPORT")
    print("=" * 60)
    print(f"History source: {stats['history_file']}")
    print(f"Output file:   {stats['output_file']}")
    print(f"Records seen:  {stats['records_seen']}")
    print(f"Exported:      {stats['exported']}")
    print(f"Skipped failed:      {stats['excluded_failures']}")
    print(f"Skipped malformed:   {stats['excluded_malformed']}")
    print(f"Skipped unsafe:      {stats['excluded_unsafe']}")
    print(f"Skipped too short:   {stats['excluded_too_short']}")

    if stats["exported"] == 0:
        print("WARNING: No eligible traces were exported. Run more successful tasks first.")

    return stats


def auto_diagnose(results: list[dict]) -> str:
    """Analyze eval outcomes and surface likely systemic fixes."""
    if not results:
        return "No results captured."
    timeout_count = sum(1 for item in results if "timed out" in str(item.get("final_output", "")).lower())
    format_fail_count = sum(1 for item in results if bool(item.get("format_failure")))
    issues: list[str] = []
    if timeout_count > len(results) * 0.3:
        issues.append(
            "HIGH TIMEOUT RATE: Verify startup_check output, reduce num_ctx, and confirm model is warm in Ollama."
        )
    if format_fail_count > len(results) * 0.3:
        issues.append(
            "HIGH FORMAT FAILURE: Rebuild voco-agent model and validate prompt/system contract outputs."
        )
    return "\n".join(issues) if issues else "No obvious systemic issues found."


def evaluate(*, prompts: list[dict] | None = None, run_startup: bool = True, pause_seconds: float = 1.0) -> dict:
    """Run reliability suite and return report."""
    selected_prompts = prompts if prompts is not None else TEST_PROMPTS
    if run_startup:
        run_startup_check(autofix=True, strict=False)

    print("=" * 60)
    print("VOCO EVALUATION SUITE")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print(f"Total prompts: {len(selected_prompts)}")
    print("=" * 60)

    results: list[dict] = []

    for index, test in enumerate(selected_prompts, start=1):
        prompt = test["prompt"]
        category = test["category"]
        expected_path = test["expected_path"]

        print(f"\n[{index}/{len(selected_prompts)}] Category: {category}")
        print(f"  Prompt: {prompt}")

        context = AgentContext()
        messages_emitted: list[dict] = []

        def capture_message(message: str, level: str) -> None:
            messages_emitted.append({"msg": message, "level": level})
            if level == "step":
                print(f"  -> {message}")

        start = time.time()
        try:
            final_output = orchestrator_run(task=prompt, context=context, ui_callback=capture_message)
            elapsed = round(time.time() - start, 1)

            success_messages = [m for m in messages_emitted if m["level"] == "success"]
            error_messages = [m for m in messages_emitted if m["level"] == "error"]
            format_failure = any("format issue" in m["msg"].lower() for m in messages_emitted)
            success = len(success_messages) > 0 and "OK" in final_output

            result = {
                "prompt": prompt,
                "category": category,
                "expected_path": expected_path,
                "success": success,
                "elapsed_seconds": elapsed,
                "format_failure": format_failure,
                "final_output": final_output[:200],
                "steps_executed": len([m for m in messages_emitted if m["level"] == "step"]),
                "error_count": len(error_messages),
            }
            print(f"  {'OK' if success else 'FAIL'} {'Success' if success else 'Failed'} in {elapsed}s")
        except Exception as exc:
            elapsed = round(time.time() - start, 1)
            result = {
                "prompt": prompt,
                "category": category,
                "expected_path": expected_path,
                "success": False,
                "elapsed_seconds": elapsed,
                "format_failure": False,
                "final_output": f"EXCEPTION: {exc}",
                "steps_executed": 0,
                "error_count": 1,
            }
            print(f"  FAIL EXCEPTION: {exc}")

        results.append(result)
        time.sleep(max(0.0, float(pause_seconds)))

    total = len(results)
    successes = sum(1 for result in results if result["success"])
    format_failures = sum(1 for result in results if result["format_failure"])
    avg_elapsed = sum(result["elapsed_seconds"] for result in results) / total

    by_category = {}
    category_order = sorted({str(item.get("category", "unknown")) for item in selected_prompts})
    for category in category_order:
        category_results = [result for result in results if result["category"] == category]
        success_count = sum(1 for result in category_results if result["success"])
        by_category[category] = {
            "total": len(category_results),
            "successes": success_count,
            "rate": round((success_count / len(category_results) * 100), 1) if category_results else 0.0,
        }

    print("\n" + "=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)
    print(f"Overall success rate: {successes}/{total} = {round(successes / total * 100, 1)}%")
    print(f"Format failure rate: {format_failures}/{total} = {round(format_failures / total * 100, 1)}%")
    print(f"Average task time: {round(avg_elapsed, 1)}s")
    print("\nBy category:")
    for category, stats in by_category.items():
        print(f"  {category}: {stats['successes']}/{stats['total']} = {stats['rate']}%")

    print("\nDECISION GUIDE:")
    overall_rate = successes / total * 100
    format_rate = format_failures / total * 100
    if overall_rate < 50:
        print("  WARNING: Overall success < 50% - core loop is unstable.")
    elif overall_rate < 80:
        print("  WARNING: Overall success 50-80% - prioritize weakest categories.")
    else:
        print("  OK: Overall success >= 80% - core loop is stable.")

    if format_rate > 15:
        print("  WARNING: Format failures > 15% - consider QLoRA fine-tuning.")
    else:
        print("  OK: Format failures <= 15% - prompt + parser is sufficient.")

    report = {
        "timestamp": datetime.datetime.now().isoformat(),
        "total": total,
        "successes": successes,
        "success_rate": round(successes / total * 100, 1),
        "format_failure_rate": round(format_failures / total * 100, 1),
        "avg_elapsed_seconds": round(avg_elapsed, 1),
        "by_category": by_category,
        "results": results,
    }

    report_file = f"eval_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, "w", encoding="utf-8") as file_handle:
        json.dump(report, file_handle, indent=2, ensure_ascii=False)

    print(f"\nFull report saved to: {report_file}")
    if report["success_rate"] < 50.0:
        print(auto_diagnose(results))
        print("\nSuggested next action: Run startup_check.py and verify Ollama GPU/context settings.")
    return report


def _normalize_success_threshold(value: float | None) -> float | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError("min_success_rate cannot be negative.")
    return value / 100 if value > 1 else value


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * ratio
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _run_task_step(step: BenchmarkStep) -> tuple[dict, list[str]]:
    prompt = str(step.task or "").strip()
    context = AgentContext()
    messages: list[dict] = []

    def capture_message(message: str, level: str) -> None:
        messages.append({"msg": message, "level": level})

    start = time.perf_counter()
    try:
        final_output = orchestrator_run(task=prompt, context=context, ui_callback=capture_message)
    except Exception as exc:
        elapsed = round(time.perf_counter() - start, 3)
        return (
            {
                "kind": "task",
                "task": prompt,
                "success": False,
                "latency_seconds": elapsed,
                "final_output": f"EXCEPTION: {exc}",
                "executed_tools": [],
                "executed_tool_events": [],
                "tool_statuses": [],
                "message_counts": {},
                "message": f"Task execution exception: {exc}",
            },
            [],
        )

    elapsed = round(time.perf_counter() - start, 3)
    executed_tools: list[str] = []
    executed_tool_events: list[dict[str, Any]] = []
    tool_statuses: list[str] = []
    for tool_result in context.tool_results:
        tool_name = str(tool_result.get("tool", "")).strip()
        if tool_name:
            executed_tools.append(tool_name)
        tool_args = tool_result.get("args", {})
        safe_args = dict(tool_args) if isinstance(tool_args, dict) else {}
        payload = tool_result.get("result", {})
        status = ""
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).strip().lower()
        if status:
            tool_statuses.append(status)
        if tool_name:
            executed_tool_events.append(
                {
                    "tool": tool_name,
                    "status": status or "unknown",
                    "args": safe_args,
                }
            )

    all_tools_success = bool(tool_statuses) and all(status == "success" for status in tool_statuses)
    success = final_output.startswith("[VOCO] OK") and all_tools_success

    message_counts: dict[str, int] = {}
    for message in messages:
        level = str(message.get("level", "info"))
        message_counts[level] = message_counts.get(level, 0) + 1

    return (
        {
            "kind": "task",
            "task": prompt,
            "success": success,
            "latency_seconds": elapsed,
            "final_output": final_output[:300],
            "executed_tools": executed_tools,
            "executed_tool_events": executed_tool_events,
            "tool_statuses": tool_statuses,
            "message_counts": message_counts,
            "message": final_output[:300],
        },
        executed_tools,
    )


def _run_tool_step(step: BenchmarkStep) -> tuple[dict, list[str]]:
    tool_name = str(step.tool or "").strip()
    args = dict(step.args or {})
    start = time.perf_counter()
    result = dispatch_tool(tool_name, args)
    elapsed = round(time.perf_counter() - start, 3)
    result_status = str(result.get("status", "error")).strip() or "error"
    message = str(result.get("message", ""))
    success = result_status == "success"
    return (
        {
            "kind": "tool",
            "tool": tool_name,
            "args": args,
            "success": success,
            "latency_seconds": elapsed,
            "result_status": result_status,
            "executed_tool_events": (
                [{"tool": tool_name, "status": result_status.lower(), "args": dict(args)}] if tool_name else []
            ),
            "message": message[:300],
        },
        [tool_name] if tool_name else [],
    )


def _normalize_open_app_target(raw_args: dict[str, Any]) -> str:
    for key in ("app_name", "app", "name", "window_title"):
        value = raw_args.get(key)
        normalized = str(value or "").strip().lower()
        if normalized:
            return normalized
    return ""


def _find_duplicate_open_app_targets(
    tool_events: list[dict[str, Any]],
    *,
    allowed_targets: set[str],
) -> list[str]:
    seen_targets: set[str] = set()
    duplicate_targets: set[str] = set()

    for event in tool_events:
        tool_name = str(event.get("tool", "")).strip().lower()
        if tool_name != "open_app":
            continue
        status = str(event.get("status", "")).strip().lower()
        if status and status != "success":
            continue
        raw_args = event.get("args", {})
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        if bool(args.get("force_new")):
            continue
        target = _normalize_open_app_target(args)
        if not target or target in allowed_targets:
            continue
        if target in seen_targets:
            duplicate_targets.add(target)
        else:
            seen_targets.add(target)

    return sorted(duplicate_targets)


def _run_benchmark_scenario(scenario: BenchmarkScenario) -> dict:
    print(f"\nScenario: {scenario.scenario_id} [{scenario.category}]")
    print(f"  {scenario.description}")

    start = time.perf_counter()
    step_results: list[dict] = []
    executed_tools: list[str] = []
    executed_tool_events: list[dict[str, Any]] = []
    step_failure_reason: str | None = None

    for idx, step in enumerate(scenario.steps, start=1):
        if step.kind == "task":
            step_result, step_tools = _run_task_step(step)
            label = f"task: {step.task}"
        elif step.kind == "tool":
            step_result, step_tools = _run_tool_step(step)
            label = f"tool: {step.tool}"
        else:
            step_result = {
                "kind": step.kind,
                "success": False,
                "latency_seconds": 0.0,
                "message": f"Unsupported benchmark step kind: {step.kind}",
            }
            step_tools = []
            label = f"invalid-step: {step.kind}"

        step_results.append(step_result)
        executed_tools.extend(step_tools)
        raw_step_events = step_result.get("executed_tool_events", [])
        if isinstance(raw_step_events, list):
            for raw_event in raw_step_events:
                if isinstance(raw_event, dict):
                    executed_tool_events.append(raw_event)
        step_status = "OK" if step_result["success"] else "FAIL"
        print(f"  - [{idx}/{len(scenario.steps)}] {step_status} {label} ({step_result['latency_seconds']}s)")

        if not step_result["success"]:
            step_failure_reason = str(step_result.get("message", "Benchmark step failed."))
            break

    planned_steps = len(scenario.steps)
    completed_steps = len(step_results)
    all_steps_completed = completed_steps == planned_steps
    all_step_results_successful = all(bool(step_result.get("success")) for step_result in step_results) and all_steps_completed
    step_completion_percent = 100.0 if planned_steps == 0 else round((completed_steps / planned_steps) * 100, 2)

    tool_sequence = [str(event.get("tool", "")).strip() for event in executed_tool_events]
    tool_sequence = [tool_name for tool_name in tool_sequence if tool_name]
    if not tool_sequence and executed_tools:
        tool_sequence = [tool_name for tool_name in executed_tools if tool_name]
    unique_tools = sorted(set(tool_sequence))
    missing_required_tools = sorted(tool for tool in scenario.required_tools if tool not in unique_tools)
    required_tool_coverage = (
        1.0
        if not scenario.required_tools
        else (len(scenario.required_tools) - len(missing_required_tools)) / len(scenario.required_tools)
    )
    tool_coverage_passed = len(missing_required_tools) == 0

    completion_indicators = scenario.completion_indicators or scenario.required_tools
    successful_tools = {
        str(event.get("tool", "")).strip()
        for event in executed_tool_events
        if str(event.get("status", "")).strip().lower() == "success"
    }
    missing_completion_indicators = sorted(
        indicator for indicator in completion_indicators if indicator not in successful_tools
    )
    completion_indicator_coverage = (
        1.0
        if not completion_indicators
        else (len(completion_indicators) - len(missing_completion_indicators)) / len(completion_indicators)
    )

    completion_output_markers = tuple(
        marker.strip()
        for marker in scenario.completion_output_markers
        if str(marker).strip()
    )
    output_haystack = " ".join(
        (
            f"{str(step_result.get('final_output', '')).strip()} "
            f"{str(step_result.get('message', '')).strip()}"
        ).strip()
        for step_result in step_results
    ).lower()
    missing_completion_output_markers = sorted(
        marker for marker in completion_output_markers if marker.lower() not in output_haystack
    )
    completion_output_marker_coverage = (
        1.0
        if not completion_output_markers
        else (len(completion_output_markers) - len(missing_completion_output_markers))
        / len(completion_output_markers)
    )

    completion_artifact_paths = tuple(
        str(path).strip()
        for path in scenario.completion_artifact_paths
        if str(path).strip()
    )
    missing_completion_artifact_paths = sorted(
        path for path in completion_artifact_paths if not Path(path).exists()
    )
    completion_artifact_coverage = (
        1.0
        if not completion_artifact_paths
        else (len(completion_artifact_paths) - len(missing_completion_artifact_paths))
        / len(completion_artifact_paths)
    )

    allowed_duplicate_targets = {
        str(target).strip().lower() for target in scenario.allow_duplicate_open_app_targets if str(target).strip()
    }
    duplicate_open_app_targets = _find_duplicate_open_app_targets(
        executed_tool_events,
        allowed_targets=allowed_duplicate_targets,
    )
    duplicate_open_app_check_passed = not scenario.enforce_no_duplicate_open_app or not duplicate_open_app_targets
    completion_state_checks = {
        "all_steps_completed": all_steps_completed,
        "all_step_results_successful": all_step_results_successful,
        "no_duplicate_open_app": duplicate_open_app_check_passed,
    }

    completion_oracle_failures: list[str] = []
    if not all_steps_completed:
        completion_oracle_failures.append(
            f"Only completed {completed_steps}/{planned_steps} planned steps."
        )
    if not all_step_results_successful:
        completion_oracle_failures.append("One or more executed steps failed.")
    if missing_completion_indicators:
        completion_oracle_failures.append(
            "Missing completion indicators: " + ", ".join(missing_completion_indicators)
        )
    if missing_completion_output_markers:
        completion_oracle_failures.append(
            "Missing completion output markers: " + ", ".join(missing_completion_output_markers)
        )
    if missing_completion_artifact_paths:
        completion_oracle_failures.append(
            "Missing completion artifacts: " + ", ".join(missing_completion_artifact_paths)
        )
    if scenario.enforce_no_duplicate_open_app and not duplicate_open_app_check_passed:
        completion_oracle_failures.append(
            "Duplicate open_app target(s) detected: " + ", ".join(duplicate_open_app_targets)
        )
    completion_oracle_passed = len(completion_oracle_failures) == 0

    failure_type: str | None = None
    failure_reason: str | None = None
    if step_failure_reason is not None:
        failure_type = "step_execution"
        failure_reason = step_failure_reason
    elif not tool_coverage_passed:
        failure_type = "tool_coverage"
        failure_reason = "Missing expected tool coverage: " + ", ".join(missing_required_tools)
    elif not completion_oracle_passed:
        failure_type = "completion_oracle"
        failure_reason = "Completion oracle failed: " + "; ".join(completion_oracle_failures)

    latency = round(time.perf_counter() - start, 3)
    success = failure_type is None
    if success:
        print(f"  -> OK in {latency}s")
    else:
        print(f"  -> FAIL in {latency}s | {failure_reason}")

    assertions = {
        "required_tool_coverage": tool_coverage_passed,
        "completion_indicators": len(missing_completion_indicators) == 0,
        "completion_output_markers": len(missing_completion_output_markers) == 0,
        "completion_artifact_paths": len(missing_completion_artifact_paths) == 0,
        "no_duplicate_open_app": duplicate_open_app_check_passed,
        "all_steps_completed": all_steps_completed,
        "all_step_results_successful": all_step_results_successful,
        "completion_oracle_passed": completion_oracle_passed,
    }

    return {
        "id": scenario.scenario_id,
        "category": scenario.category,
        "description": scenario.description,
        "success": success,
        "latency_seconds": latency,
        "required_tools": list(scenario.required_tools),
        "completion_indicators": list(completion_indicators),
        "completion_output_markers": list(completion_output_markers),
        "completion_artifact_paths": list(completion_artifact_paths),
        "executed_tools": unique_tools,
        "tool_sequence": tool_sequence,
        "missing_required_tools": missing_required_tools,
        "missing_completion_indicators": missing_completion_indicators,
        "missing_completion_output_markers": missing_completion_output_markers,
        "missing_completion_artifact_paths": missing_completion_artifact_paths,
        "duplicate_open_app_targets": duplicate_open_app_targets,
        "tool_coverage_passed": tool_coverage_passed,
        "completion_oracle_passed": completion_oracle_passed,
        "completion_oracle": {
            "passed": completion_oracle_passed,
            "failures": completion_oracle_failures,
            "state_checks": completion_state_checks,
            "expected": {
                "indicators": list(completion_indicators),
                "output_markers": list(completion_output_markers),
                "artifact_paths": list(completion_artifact_paths),
            },
            "missing": {
                "indicators": missing_completion_indicators,
                "output_markers": missing_completion_output_markers,
                "artifact_paths": missing_completion_artifact_paths,
            },
        },
        "assertions": assertions,
        "metrics": {
            "tool_event_count": len(tool_sequence),
            "required_tool_coverage_percent": round(required_tool_coverage * 100, 2),
            "completion_indicator_coverage_percent": round(completion_indicator_coverage * 100, 2),
            "completion_output_marker_coverage_percent": round(completion_output_marker_coverage * 100, 2),
            "completion_artifact_coverage_percent": round(completion_artifact_coverage * 100, 2),
            "planned_steps": planned_steps,
            "completed_steps": completed_steps,
            "step_completion_percent": step_completion_percent,
            "completion_oracle_failure_count": len(completion_oracle_failures),
        },
        "failure_type": failure_type,
        "failure_reason": failure_reason,
        "steps": step_results,
    }


def _select_scenarios(
    categories: list[str] | None = None,
    scenario_ids: list[str] | None = None,
) -> list[BenchmarkScenario]:
    selected = list(BENCHMARK_SCENARIOS)

    if categories:
        category_set = {category.strip() for category in categories if category.strip()}
        selected = [scenario for scenario in selected if scenario.category in category_set]

    if scenario_ids:
        requested = {scenario_id.strip() for scenario_id in scenario_ids if scenario_id.strip()}
        known = {scenario.scenario_id for scenario in BENCHMARK_SCENARIOS}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"Unknown benchmark scenario(s): {', '.join(unknown)}")
        selected = [scenario for scenario in selected if scenario.scenario_id in requested]

    if not selected:
        raise ValueError("No benchmark scenarios selected. Use --list-scenarios to inspect options.")

    return selected


def list_benchmark_scenarios() -> None:
    print("Available benchmark scenarios:")
    for scenario in BENCHMARK_SCENARIOS:
        required = ", ".join(scenario.required_tools) if scenario.required_tools else "none"
        print(f"  - {scenario.scenario_id} [{scenario.category}]")
        print(f"      {scenario.description}")
        print(f"      required_tools: {required}")


def list_stress_capability_baseline() -> None:
    print("Stress capability baseline scenarios:")
    for scenario in STRESS_CAPABILITY_BASELINE:
        expected_tools = ", ".join(scenario.expected_tools) if scenario.expected_tools else "none"
        print(f"  - {scenario.scenario_id} [{scenario.priority} | {scenario.support_level}]")
        print(f"      flow: {scenario.requested_flow}")
        print(f"      probe: {scenario.verification_prompt}")
        print(f"      expected_tools: {expected_tools}")
        print(f"      key_gaps: {' | '.join(scenario.key_gaps)}")


def run_benchmark(
    *,
    categories: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    min_success_rate: float | None = 80.0,
    max_avg_latency: float | None = None,
    max_p95_latency: float | None = None,
    output_path: str | None = None,
    enforce_gate: bool = True,
) -> dict:
    selected_scenarios = _select_scenarios(categories=categories, scenario_ids=scenario_ids)
    min_success_ratio = _normalize_success_threshold(min_success_rate)

    if max_avg_latency is not None and max_avg_latency <= 0:
        raise ValueError("max_avg_latency must be > 0 when provided.")
    if max_p95_latency is not None and max_p95_latency <= 0:
        raise ValueError("max_p95_latency must be > 0 when provided.")

    print("=" * 60)
    print("VOCO BENCHMARK SUITE")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print(f"Scenarios: {len(selected_scenarios)}")
    print("=" * 60)

    scenario_results: list[dict] = []
    for index, scenario in enumerate(selected_scenarios, start=1):
        print(f"\n[{index}/{len(selected_scenarios)}]")
        scenario_results.append(_run_benchmark_scenario(scenario))

    total = len(scenario_results)
    successes = sum(1 for result in scenario_results if result["success"])
    success_rate_ratio = (successes / total) if total else 0.0
    success_rate_percent = round(success_rate_ratio * 100, 2)

    latencies = [float(result["latency_seconds"]) for result in scenario_results]
    avg_latency = statistics.mean(latencies) if latencies else 0.0
    median_latency = statistics.median(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 0.95) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0

    by_category: dict[str, dict] = {}
    for category in BENCHMARK_CATEGORIES:
        category_results = [result for result in scenario_results if result["category"] == category]
        if not category_results:
            continue
        category_successes = sum(1 for result in category_results if result["success"])
        category_latencies = [float(result["latency_seconds"]) for result in category_results]
        by_category[category] = {
            "total": len(category_results),
            "successes": category_successes,
            "success_rate_percent": round(category_successes / len(category_results) * 100, 2),
            "avg_latency_seconds": round(statistics.mean(category_latencies), 3),
            "p95_latency_seconds": round(_percentile(category_latencies, 0.95), 3),
        }

    tool_coverage_failure_ids = sorted(
        result["id"] for result in scenario_results if not bool(result.get("tool_coverage_passed", True))
    )
    completion_oracle_failure_ids = sorted(
        result["id"] for result in scenario_results if not bool(result.get("completion_oracle_passed", True))
    )
    failure_type_counts: dict[str, int] = {}
    for result in scenario_results:
        raw_failure_type = result.get("failure_type")
        if raw_failure_type is None:
            continue
        failure_type = str(raw_failure_type).strip()
        if not failure_type:
            continue
        failure_type_counts[failure_type] = failure_type_counts.get(failure_type, 0) + 1

    gate_failures: list[str] = []
    if min_success_ratio is not None and success_rate_ratio < min_success_ratio:
        gate_failures.append(
            f"Success rate {success_rate_percent:.2f}% is below minimum {min_success_ratio * 100:.2f}%."
        )
    if max_avg_latency is not None and avg_latency > max_avg_latency:
        gate_failures.append(
            f"Average latency {avg_latency:.3f}s exceeds max {max_avg_latency:.3f}s."
        )
    if max_p95_latency is not None and p95_latency > max_p95_latency:
        gate_failures.append(
            f"P95 latency {p95_latency:.3f}s exceeds max {max_p95_latency:.3f}s."
        )

    gate_passed = (len(gate_failures) == 0) if enforce_gate else True

    report = {
        "timestamp": datetime.datetime.now().isoformat(),
        "suite": "benchmark",
        "scenario_count": total,
        "successes": successes,
        "failures": total - successes,
        "success_rate_percent": success_rate_percent,
        "latency_stats": {
            "avg_seconds": round(avg_latency, 3),
            "median_seconds": round(median_latency, 3),
            "p95_seconds": round(p95_latency, 3),
            "min_seconds": round(min_latency, 3),
            "max_seconds": round(max_latency, 3),
        },
        "failure_breakdown": {
            "tool_coverage_failures": {
                "count": len(tool_coverage_failure_ids),
                "scenario_ids": tool_coverage_failure_ids,
            },
            "completion_oracle_failures": {
                "count": len(completion_oracle_failure_ids),
                "scenario_ids": completion_oracle_failure_ids,
            },
            "by_failure_type": failure_type_counts,
        },
        "by_category": by_category,
        "gate": {
            "enforced": enforce_gate,
            "passed": gate_passed,
            "thresholds": {
                "min_success_rate_percent": round(min_success_ratio * 100, 2)
                if min_success_ratio is not None
                else None,
                "max_avg_latency_seconds": max_avg_latency,
                "max_p95_latency_seconds": max_p95_latency,
            },
            "failures": gate_failures,
        },
        "results": scenario_results,
    }

    report_file = output_path or f"benchmark_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, "w", encoding="utf-8") as file_handle:
        json.dump(report, file_handle, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)
    print(f"Success rate: {successes}/{total} = {success_rate_percent:.2f}%")
    print(
        "Latency (s): "
        f"avg={avg_latency:.3f}, median={median_latency:.3f}, p95={p95_latency:.3f}, max={max_latency:.3f}"
    )
    print(
        f"Tool coverage failures: {len(tool_coverage_failure_ids)}"
        + (f" [{', '.join(tool_coverage_failure_ids)}]" if tool_coverage_failure_ids else "")
    )
    print(
        f"Completion oracle failures: {len(completion_oracle_failure_ids)}"
        + (f" [{', '.join(completion_oracle_failure_ids)}]" if completion_oracle_failure_ids else "")
    )
    if gate_failures:
        print("Gate checks:")
        for failure in gate_failures:
            print(f"  FAIL {failure}")
    else:
        print("Gate checks: OK")

    print(f"Report saved to: {report_file}")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VOCO evaluation + benchmark runner.")
    subparsers = parser.add_subparsers(dest="command")

    suite_parser = subparsers.add_parser("suite", help="Run reliability evaluation suite.")
    suite_parser.add_argument(
        "--fast",
        action="store_true",
        help="Run reduced 10-prompt fast suite.",
    )

    benchmark_parser = subparsers.add_parser("benchmark", help="Run benchmark scenarios with regression gate.")
    benchmark_parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List benchmark scenarios and exit.",
    )
    benchmark_parser.add_argument(
        "--list-stress-baseline",
        action="store_true",
        help="List stress-capability audit baseline scenarios and exit.",
    )
    benchmark_parser.add_argument(
        "--category",
        action="append",
        choices=BENCHMARK_CATEGORIES,
        help="Filter benchmark by category (repeatable).",
    )
    benchmark_parser.add_argument(
        "--scenario",
        action="append",
        help="Filter benchmark by scenario id (repeatable).",
    )
    benchmark_parser.add_argument(
        "--min-success-rate",
        type=float,
        default=80.0,
        help="Minimum success rate for gate (0-1 ratio or 0-100 percent).",
    )
    benchmark_parser.add_argument(
        "--max-avg-latency",
        type=float,
        default=None,
        help="Maximum average scenario latency in seconds.",
    )
    benchmark_parser.add_argument(
        "--max-p95-latency",
        type=float,
        default=None,
        help="Maximum p95 scenario latency in seconds.",
    )
    benchmark_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional JSON report output path.",
    )
    benchmark_parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Do not fail process when thresholds are missed.",
    )

    export_parser = subparsers.add_parser(
        "export-learning-data",
        help="Export curated action traces from HISTORY.jsonl to SFT JSONL.",
    )
    export_parser.add_argument(
        "--history",
        type=str,
        default=HISTORY_FILE,
        help="Path to source HISTORY.jsonl file.",
    )
    export_parser.add_argument(
        "--output",
        type=str,
        default=str(Path("memory") / "vault" / "sft_command_traces.jsonl"),
        help="Target JSONL path for exported SFT samples.",
    )
    export_parser.add_argument(
        "--min-steps",
        type=int,
        default=1,
        help="Minimum action-trace step count required to keep a sample.",
    )
    export_parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Optional cap on processed history records.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "suite"

    if command == "suite":
        run_fast = bool(getattr(args, "fast", False))
        evaluate(
            prompts=FAST_EVAL_PROMPTS if run_fast else TEST_PROMPTS,
            run_startup=True,
            pause_seconds=0.3 if run_fast else 1.0,
        )
        return 0

    if command == "benchmark":
        if args.list_stress_baseline:
            list_stress_capability_baseline()
            return 0
        if args.list_scenarios:
            list_benchmark_scenarios()
            return 0
        try:
            report = run_benchmark(
                categories=args.category,
                scenario_ids=args.scenario,
                min_success_rate=args.min_success_rate,
                max_avg_latency=args.max_avg_latency,
                max_p95_latency=args.max_p95_latency,
                output_path=args.output,
                enforce_gate=not args.no_gate,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        if report["gate"]["passed"]:
            return 0
        print("REGRESSION GATE FAILED.")
        return 1

    if command == "export-learning-data":
        try:
            export_learning_data(
                history_path=args.history,
                output_path=args.output,
                min_steps=args.min_steps,
                max_records=args.max_records,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
