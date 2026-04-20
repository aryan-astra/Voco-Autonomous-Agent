from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_XML_PATH = Path(r"O:\Coding-proj\Sem4-AIOT\voconew.xml")
DEFAULT_TARGET = 200
MIN_TARGET = 100
MAX_TARGET = 300
GENERIC_AUGMENT_LIMIT = 16
SYSTEM_PROMPT = (
    "You are a VOCO assistant that must follow a strict XML protocol.\n"
    "Output exactly one block: either <tool_call>...</tool_call> or <final>...</final>.\n"
    "Tool-call payload must be strict JSON with this shape: "
    '{"name":"tool_name","args":{...}}.\n'
    "Do not output any extra text."
)


@dataclass(frozen=True)
class Example:
    prompt: str
    completion: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build VOCO XML-tool protocol training data in HuggingFace datasets format."
    )
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Example count (100-300).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("voco_training_data"),
        help="Output directory for Dataset.save_to_disk().",
    )
    parser.add_argument(
        "--xml-path",
        type=Path,
        default=DEFAULT_XML_PATH,
        help="Path to voconew.xml source.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic generation.")
    args = parser.parse_args()
    if not MIN_TARGET <= args.target <= MAX_TARGET:
        parser.error(f"--target must be between {MIN_TARGET} and {MAX_TARGET}.")
    return args


def extract_available_tools(xml_text: str) -> list[str]:
    marker = "AVAILABLE TOOLS (exact names):"
    marker_idx = xml_text.find(marker)
    if marker_idx == -1:
        raise ValueError(f"Could not find '{marker}' in voconew.xml.")

    tools: list[str] = []
    started = False
    for raw_line in xml_text[marker_idx + len(marker) :].splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            tool_name = line[2:].strip()
            if re.fullmatch(r"[A-Za-z0-9_]+", tool_name):
                tools.append(tool_name)
            started = True
            continue
        if started:
            break

    unique_tools = sorted(dict.fromkeys(tools))
    if not unique_tools:
        raise ValueError("No tool names were extracted from the AVAILABLE TOOLS section.")
    return unique_tools


def format_tool_call(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"name": name, "args": args}, ensure_ascii=False, separators=(",", ":"))
    return f"<tool_call>\n{payload}\n</tool_call>"


def format_final(text: str) -> str:
    return f"<final>{text}</final>"


def render_prompt(
    user_content: str,
    *,
    prior_assistant_tool_call: str | None = None,
    tool_result: dict[str, Any] | None = None,
) -> str:
    segments = [f"<|im_start|>system\n{SYSTEM_PROMPT}\n<|im_end|>"]
    segments.append(f"<|im_start|>user\n{user_content}\n<|im_end|>")
    if prior_assistant_tool_call is not None:
        segments.append(f"<|im_start|>assistant\n{prior_assistant_tool_call}\n<|im_end|>")
    if tool_result is not None:
        result_json = json.dumps(tool_result, ensure_ascii=False, separators=(",", ":"))
        segments.append(f"<|im_start|>user\n<tool_result>\n{result_json}\n</tool_result>\n<|im_end|>")
    segments.append("<|im_start|>assistant")
    return "\n".join(segments)


def add_generic_tool_examples(
    examples: list[Example],
    available: set[str],
    covered_tools: set[str],
    limit: int = GENERIC_AUGMENT_LIMIT,
) -> None:
    if limit <= 0:
        return
    missing_tools = sorted(available - covered_tools)
    for tool_name in missing_tools[:limit]:
        request = (
            f"Use the {tool_name} tool for this request. "
            f"If no specific arguments are known, call it with empty args."
        )
        examples.append(
            Example(
                prompt=render_prompt(request),
                completion=format_tool_call(tool_name, {}),
            )
        )


def build_direct_pool(available: set[str]) -> list[Example]:
    examples: list[Example] = []
    covered_tools: set[str] = set()

    def add(tool_name: str, prompt: str, args: dict[str, Any]) -> None:
        if tool_name not in available:
            return
        covered_tools.add(tool_name)
        examples.append(
            Example(
                prompt=render_prompt(prompt),
                completion=format_tool_call(tool_name, args),
            )
        )

    for path in (
        r"C:\Users\User\Desktop",
        r"C:\Users\User\Documents",
        r"O:\Coding-proj\Sem4-AIOT",
    ):
        add("list_files", f"List files inside {path}.", {"directory": path, "depth": 2})
        add("list_files", f"Show me what's in {path}.", {"directory": path, "depth": 2})

    for path in (
        r"C:\Users\User\Desktop\todo.txt",
        r"C:\Users\User\Documents\project_notes.md",
        r"O:\Coding-proj\Sem4-AIOT\README.md",
    ):
        add("read_file", f"Open and read {path}.", {"path": path})
        add("read_file", f"Read file content from {path}.", {"path": path})

    write_targets = (
        (r"C:\Users\User\Desktop\daily_plan.txt", "09:00 standup\n11:00 code review"),
        (r"C:\Users\User\Documents\meeting_notes.txt", "Action items:\n- finalize report\n- send update"),
        (r"O:\Coding-proj\Sem4-AIOT\workspace\scratch.txt", "temporary scratch output"),
    )
    for path, content in write_targets:
        add("write_file", f"Write notes to {path}.", {"path": path, "content": content})
        add("write_file", f"Create {path} with this text.", {"path": path, "content": content})

    for app_name in ("notepad", "calculator", "chrome", "file explorer", "powershell"):
        add("open_app", f"Open {app_name}.", {"app_name": app_name})
        add("open_app", f"Launch {app_name} now.", {"app_name": app_name})

    for url in (
        "https://github.com",
        "https://duckduckgo.com",
        "https://news.ycombinator.com",
        "https://docs.python.org/3/",
        "https://chat.openai.com",
    ):
        add("browser_navigate", f"Go to {url}.", {"url": url})
        add("browser_navigate", f"Navigate browser to {url}.", {"url": url})

    browser_type_actions = (
        ("Type 'github copilot cli docs' into the active search box and submit.", {"text": "github copilot cli docs", "submit": True}),
        ("Write 'hello from voco' in the current text field.", {"text": "hello from voco", "submit": False}),
        ("Enter multiline note 'Line 1\\nLine 2' in the current compose box.", {"text": "Line 1\nLine 2", "multiline": True, "submit": False}),
        ("Type 'windows automation shortcuts' and press enter.", {"text": "windows automation shortcuts", "submit": True}),
    )
    for prompt, args in browser_type_actions:
        add("browser_type", prompt, args)
        add("browser_type", f"In browser: {prompt}", args)

    browser_click_actions = (
        ("Click the Sign in button in the browser.", {"element_name": "Sign in button"}),
        ("Click the first search result.", {"element_name": "result", "occurrence": 1}),
        ("Click the Learn more link.", {"element_name": "Learn more link"}),
        ("Click the Send button.", {"element_name": "Send button"}),
    )
    for prompt, args in browser_click_actions:
        add("browser_click", prompt, args)
        add("browser_click", f"Browser action: {prompt}", args)

    search_requests = (
        ("Find files related to quarterly report.", {"query": "quarterly report"}),
        ("Search for a file named budget.xlsx.", {"query": "budget.xlsx"}),
        ("Locate notes mentioning deployment.", {"query": "deployment notes"}),
        ("Find python scripts for scraping.", {"query": "scraper .py"}),
    )
    for prompt, args in search_requests:
        add("search_file", prompt, args)
        add("search_file", f"Run local file search: {prompt}", args)

    command_requests = (
        ("Run Get-Date and show output.", {"command": "Get-Date"}),
        ("Check running processes with 'Get-Process | Select-Object -First 5'.", {"command": "Get-Process | Select-Object -First 5"}),
        ("Run 'whoami' command.", {"command": "whoami"}),
        ("Get current directory using pwd.", {"command": "pwd"}),
    )
    for prompt, args in command_requests:
        add("run_command", prompt, args)
        add("run_command", f"Execute this command safely: {prompt}", args)

    save_requests = (
        ("Save 'Meeting moved to 3 PM' as desktop file meeting_update.txt.", {"content": "Meeting moved to 3 PM", "filename": "meeting_update.txt"}),
        ("Create a desktop reminder file todo_today.txt with text 'Send invoice'.", {"content": "Send invoice", "filename": "todo_today.txt"}),
        ("Store this quote to desktop file quote.txt: 'Stay focused'.", {"content": "Stay focused", "filename": "quote.txt"}),
        ("Save clipboard summary into a desktop file.", {"from_clipboard": True}),
    )
    for prompt, args in save_requests:
        add("save_text_to_desktop_file", prompt, args)
        add("save_text_to_desktop_file", f"Desktop save task: {prompt}", args)

    screenshot_requests = (
        ("Take a screenshot and save it as desktop_capture.png.", {"filename": "desktop_capture.png"}),
        ("Capture current screen into meeting_screen.png.", {"filename": "meeting_screen.png"}),
        ("Take screenshot now for bug report screenshot_bug.png.", {"filename": "screenshot_bug.png"}),
    )
    for prompt, args in screenshot_requests:
        add("take_screenshot", prompt, args)
        add("take_screenshot", f"Screenshot request: {prompt}", args)

    add("mute_audio", "Mute the system audio.", {"mute": True})
    add("mute_audio", "Silence my PC sound immediately.", {"mute": True})
    add("mute_audio", "Unmute the system volume now.", {"mute": False})

    add_generic_tool_examples(examples, available, covered_tools)

    return examples


def build_followup_pool(available: set[str]) -> list[Example]:
    examples: list[Example] = []

    def add(tool_name: str, request: str, args: dict[str, Any], tool_result: dict[str, Any], final_text: str) -> None:
        if tool_name not in available:
            return
        tool_call = format_tool_call(tool_name, args)
        prompt = render_prompt(
            request,
            prior_assistant_tool_call=tool_call,
            tool_result=tool_result,
        )
        examples.append(Example(prompt=prompt, completion=format_final(final_text)))

    add(
        "list_files",
        r"Show desktop files.",
        {"directory": r"C:\Users\User\Desktop", "depth": 2},
        {"ok": True, "files": ["todo.txt", "budget.xlsx", "meeting_notes.docx"]},
        "Found 3 files on Desktop: todo.txt, budget.xlsx, meeting_notes.docx.",
    )
    add(
        "read_file",
        r"Read C:\Users\User\Desktop\todo.txt.",
        {"path": r"C:\Users\User\Desktop\todo.txt"},
        {"ok": True, "content": "1) Send report\n2) Call Alex"},
        "Here is the file content: 1) Send report 2) Call Alex.",
    )
    add(
        "write_file",
        r"Write deployment notes to C:\Users\User\Documents\deploy.txt.",
        {"path": r"C:\Users\User\Documents\deploy.txt", "content": "Deploy at 9 PM"},
        {"ok": True, "path": r"C:\Users\User\Documents\deploy.txt"},
        r"Done. I wrote the content to C:\Users\User\Documents\deploy.txt.",
    )
    add(
        "open_app",
        "Open Notepad.",
        {"app_name": "notepad"},
        {"ok": True, "app_name": "notepad", "window_title": "Untitled - Notepad"},
        "Notepad is open and ready.",
    )
    add(
        "browser_navigate",
        "Open github.com in browser.",
        {"url": "https://github.com"},
        {"ok": True, "url": "https://github.com", "title": "GitHub · Build and ship software"},
        "Navigated to GitHub successfully.",
    )
    add(
        "browser_type",
        "Search for windows automation tips.",
        {"text": "windows automation tips", "submit": True},
        {"ok": True, "typed": "windows automation tips", "submitted": True},
        "Typed the query and submitted the search.",
    )
    add(
        "browser_click",
        "Click the first result.",
        {"element_name": "result", "occurrence": 1},
        {"ok": True, "clicked": "result", "occurrence": 1},
        "Clicked the first result successfully.",
    )
    add(
        "search_file",
        "Find budget spreadsheet.",
        {"query": "budget.xlsx"},
        {"ok": True, "matches": [r"C:\Users\User\Documents\budget.xlsx"]},
        r"I found 1 match: C:\Users\User\Documents\budget.xlsx.",
    )
    add(
        "run_command",
        "Run Get-Date.",
        {"command": "Get-Date"},
        {"ok": True, "stdout": "Tuesday, April 08, 2026 9:22:11 AM"},
        "Command completed. Current date/time: Tuesday, April 08, 2026 9:22:11 AM.",
    )
    add(
        "save_text_to_desktop_file",
        "Save reminder text to desktop.",
        {"content": "Call client at 5 PM", "filename": "reminder.txt"},
        {"ok": True, "output_path": r"C:\Users\User\Desktop\reminder.txt"},
        r"Saved the text to C:\Users\User\Desktop\reminder.txt.",
    )
    add(
        "take_screenshot",
        "Take a screenshot.",
        {"filename": "screen.png"},
        {"ok": True, "output_path": r"C:\Users\User\Desktop\screen.png"},
        r"Screenshot captured and saved to C:\Users\User\Desktop\screen.png.",
    )
    add(
        "mute_audio",
        "Mute system audio.",
        {"mute": True},
        {"ok": True, "muted": True},
        "System audio is now muted.",
    )
    add(
        "list_files",
        r"Check O:\Coding-proj\Sem4-AIOT folder.",
        {"directory": r"O:\Coding-proj\Sem4-AIOT", "depth": 2},
        {"ok": True, "files": ["README.md", "voco_ui.py", "tools.py"]},
        "Folder contains 3 visible entries: README.md, voco_ui.py, tools.py.",
    )
    add(
        "search_file",
        "Find a file named report_final.docx.",
        {"query": "report_final.docx"},
        {"ok": False, "matches": []},
        "I couldn't find report_final.docx in indexed paths.",
    )
    add(
        "run_command",
        "Run whoami.",
        {"command": "whoami"},
        {"ok": True, "stdout": "desktop-user"},
        "Command succeeded. Current user: desktop-user.",
    )

    return examples


def expand_examples(pool: list[Example], target_count: int, rng: random.Random) -> list[dict[str, str]]:
    if target_count <= 0 or not pool:
        return []
    ordered = pool[:]
    generated: list[dict[str, str]] = []
    while len(generated) < target_count:
        rng.shuffle(ordered)
        for item in ordered:
            generated.append({"prompt": item.prompt, "completion": item.completion})
            if len(generated) >= target_count:
                break
    return generated


def build_examples(available_tools: list[str], target: int, seed: int) -> list[dict[str, str]]:
    available_set = set(available_tools)
    direct_pool = build_direct_pool(available_set)
    followup_pool = build_followup_pool(available_set)

    if not direct_pool:
        raise ValueError("No direct tool-call examples could be created from extracted tool names.")
    if not followup_pool:
        raise ValueError("No follow-up final examples could be created from extracted tool names.")

    rng = random.Random(seed)
    direct_target = max(1, int(target * 0.65))
    followup_target = target - direct_target
    direct_examples = expand_examples(direct_pool, direct_target, rng)
    followup_examples = expand_examples(followup_pool, followup_target, rng)
    examples = direct_examples + followup_examples
    rng.shuffle(examples)
    return examples


def write_jsonl(examples: list[dict[str, str]], jsonl_path: Path) -> None:
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in examples:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()

    try:
        from datasets import Dataset
    except ImportError:
        print(
            "Error: Python package 'datasets' is not installed. Install it first with "
            "'pip install datasets' and rerun this script.",
            file=sys.stderr,
        )
        return 1

    if not args.xml_path.exists():
        print(f"Error: XML file not found at {args.xml_path}", file=sys.stderr)
        return 1

    output_dir: Path = args.output_dir
    if output_dir.exists():
        print(
            f"Error: Output directory already exists: {output_dir}. "
            "Use a new --output-dir path or remove the existing directory.",
            file=sys.stderr,
        )
        return 1

    xml_text = args.xml_path.read_text(encoding="utf-8")
    try:
        tool_names = extract_available_tools(xml_text)
        examples = build_examples(tool_names, args.target, args.seed)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    dataset = Dataset.from_list(examples)
    dataset.save_to_disk(str(output_dir))

    jsonl_path = output_dir.parent / f"{output_dir.name}.jsonl"
    write_jsonl(examples, jsonl_path)

    print(
        f"Extracted {len(tool_names)} tools | generated {len(examples)} examples | "
        f"dataset: {output_dir} | jsonl: {jsonl_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
