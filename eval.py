"""
VOCO evaluation script.
Runs the 20-prompt suite and computes key reliability metrics.
Run: python eval.py
"""

from __future__ import annotations

import datetime
import json
import time

from context import AgentContext
from orchestrator import run as orchestrator_run


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


def evaluate() -> dict:
    """Run all prompts and return structured evaluation report."""
    print("=" * 60)
    print("VOCO EVALUATION SUITE")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print(f"Total prompts: {len(TEST_PROMPTS)}")
    print("=" * 60)

    results: list[dict] = []

    for index, test in enumerate(TEST_PROMPTS, start=1):
        prompt = test["prompt"]
        category = test["category"]
        expected_path = test["expected_path"]

        print(f"\n[{index}/{len(TEST_PROMPTS)}] Category: {category}")
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
        time.sleep(1)

    total = len(results)
    successes = sum(1 for result in results if result["success"])
    format_failures = sum(1 for result in results if result["format_failure"])
    avg_elapsed = sum(result["elapsed_seconds"] for result in results) / total

    by_category = {}
    for category in ["easy", "medium", "router_stress", "messy"]:
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
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\nFull report saved to: {report_file}")
    return report


if __name__ == "__main__":
    evaluate()
