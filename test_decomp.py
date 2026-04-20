#!/usr/bin/env python
"""Regression checks for decomposition + tool routing."""

from context import create_context
from orchestrator import _build_local_fastpath_plan, _build_tool_first_hybrid_plan


def emit(message: str, level: str = "info") -> None:
    print(f"[{level}] {message}")


def assert_no_local_search_misroute(route_trace: list[dict]) -> None:
    bad_routes = [item for item in route_trace if item.get("tool") == "search_local_paths"]
    assert not bad_routes, f"Unexpected search_local_paths route(s): {bad_routes}"


def run_decomposition_regression() -> None:
    prompt = (
        'open chrome in "arpit" profile and search for latest mkbhd youtube video and then '
        "paste the content in the video description into a notepad file and save the file on desktop"
    )
    context = create_context()
    result = _build_tool_first_hybrid_plan(task=prompt, context=context, emit=emit)
    assert result is not None, "Expected decomposition result, got None"
    split_steps = result.get("split_steps", [])
    route_trace = result.get("route_trace", [])
    assert len(split_steps) >= 3, f"Expected multiple split steps, got: {split_steps}"
    assert_no_local_search_misroute(route_trace)
    print("[PASS] decomposition regression prompt")


def run_youtube_comment_fastpath_regression() -> None:
    prompt = "open youtube and search java and then copy 5 comments and paste them in a notepad file"
    plan = _build_local_fastpath_plan(prompt)
    assert plan is not None, "Expected a fastpath plan for YouTube comment workflow"
    tools = [str(step.get("tool", "")).strip() for step in plan if isinstance(step, dict)]
    assert "youtube_comment_pipeline" in tools, f"Expected youtube_comment_pipeline, got tools={tools}"
    assert "search_local_paths" not in tools, f"Unexpected local search fastpath tools={tools}"
    print("[PASS] youtube comments fastpath regression prompt")


if __name__ == "__main__":
    run_decomposition_regression()
    run_youtube_comment_fastpath_regression()
    print("[PASS] all decomposition routing regressions")
