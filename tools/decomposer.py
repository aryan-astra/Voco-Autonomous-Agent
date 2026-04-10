"""Task decomposition helpers used before per-step routing."""

from __future__ import annotations

import re
from typing import Callable

from constants import MAX_STEPS
from router import should_split_task, split_task_into_steps

_INLINE_NUMBERED_STEP_REGEX = re.compile(
    r"(?:^|\s)(?:step\s*)?\d{1,2}[)\].:-]\s*(.+?)(?=(?:\s+(?:step\s*)?\d{1,2}[)\].:-]\s)|$)",
    flags=re.IGNORECASE,
)
_LINE_NUMBERED_STEP_REGEX = re.compile(r"^\s*(?:step\s*)?\d{1,2}[)\].:-]\s*(.+?)\s*$", flags=re.IGNORECASE)
_LINE_BULLET_STEP_REGEX = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_CONNECTOR_SPLIT_REGEX = re.compile(r"\b(?:and then|then|after that|afterwards|next|finally)\b|;", flags=re.IGNORECASE)
_CONNECTOR_TOKEN_REGEX = re.compile(r"\b(?:and then|then|after that|afterwards|next|finally)\b|;", flags=re.IGNORECASE)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _bounded_max_steps(max_steps: int) -> int:
    try:
        parsed = int(max_steps)
    except (TypeError, ValueError):
        parsed = MAX_STEPS
    if parsed <= 0:
        parsed = MAX_STEPS
    return min(parsed, MAX_STEPS)


def _clean_step_text(raw: str) -> str:
    cleaned = str(raw or "").strip().strip("\"'").strip()
    cleaned = re.sub(r"^[\-\*•]+\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".,;:!?")


def _sanitize_steps(candidates: list[str], max_steps: int) -> list[str]:
    bounded = _bounded_max_steps(max_steps)
    steps: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        step = _clean_step_text(candidate)
        if not step:
            continue
        if re.fullmatch(r"(?:step\s*)?\d+[)\].:-]?", step, flags=re.IGNORECASE):
            continue
        key = step.casefold()
        if key in seen:
            continue
        seen.add(key)
        steps.append(step)
        if len(steps) >= bounded:
            break

    return steps


def _parse_numbered_steps(raw_output: str, max_steps: int) -> list[str]:
    content = str(raw_output or "").strip()
    if not content:
        return []

    collected: list[str] = []
    current = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        numbered_match = _LINE_NUMBERED_STEP_REGEX.match(line)
        bullet_match = _LINE_BULLET_STEP_REGEX.match(line)
        if numbered_match:
            if current:
                collected.append(current)
            current = numbered_match.group(1).strip()
            continue
        if bullet_match:
            if current:
                collected.append(current)
            current = bullet_match.group(1).strip()
            continue
        if current:
            current = f"{current} {line}".strip()

    if current:
        collected.append(current)

    parsed = _sanitize_steps(collected, max_steps=max_steps)
    if parsed:
        return parsed

    flattened = _normalize_text(content)
    inline_steps = [match.group(1).strip() for match in _INLINE_NUMBERED_STEP_REGEX.finditer(flattened)]
    return _sanitize_steps(inline_steps, max_steps=max_steps)


def _safe_fallback_steps(text: str, max_steps: int) -> list[str]:
    content = _normalize_text(text)
    if not content:
        return []

    deterministic = _sanitize_steps(split_task_into_steps(content), max_steps=max_steps)
    if len(deterministic) > 1:
        return deterministic

    connector_chunks = [chunk for chunk in re.split(_CONNECTOR_SPLIT_REGEX, content) if chunk and chunk.strip()]
    connector_steps = _sanitize_steps(connector_chunks, max_steps=max_steps)
    if len(connector_steps) > 1:
        return connector_steps

    return [content]


def needs_decomposition(text: str) -> bool:
    content = _normalize_text(text)
    if not content:
        return False
    if should_split_task(content):
        return True
    if _INLINE_NUMBERED_STEP_REGEX.search(content):
        return True
    connector_count = len(_CONNECTOR_TOKEN_REGEX.findall(content))
    return connector_count >= 2 and len(content.split()) >= 8


def decompose_task(
    text: str,
    *,
    max_steps: int = MAX_STEPS,
    llm_decomposer: Callable[[str, int], str] | None = None,
    allow_llm_fallback: bool = True,
) -> list[str]:
    content = _normalize_text(text)
    if not content:
        return []

    bounded_steps = _bounded_max_steps(max_steps)
    deterministic_steps = _sanitize_steps(split_task_into_steps(content), max_steps=bounded_steps)
    if len(deterministic_steps) > 1:
        return deterministic_steps
    if not needs_decomposition(content):
        return [content]

    if allow_llm_fallback and llm_decomposer is not None:
        llm_output = ""
        try:
            llm_output = str(llm_decomposer(content, bounded_steps) or "").strip()
        except Exception:
            llm_output = ""
        parsed_llm_steps = _parse_numbered_steps(llm_output, max_steps=bounded_steps)
        if len(parsed_llm_steps) > 1:
            return parsed_llm_steps

    fallback_steps = _safe_fallback_steps(content, max_steps=bounded_steps)
    return fallback_steps if fallback_steps else [content]


__all__ = ["needs_decomposition", "decompose_task"]
