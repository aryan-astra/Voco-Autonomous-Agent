"""Task decomposition helpers used before per-step routing."""

from __future__ import annotations

import json
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
_COMMON_TYPO_CORRECTIONS: tuple[tuple[str, str], ...] = (
    (r"\bconments\b", "comments"),
    (r"\bcommments\b", "comments"),
    (r"\bcoments\b", "comments"),
    (r"\byoutub\b", "youtube"),
    (r"\byutube\b", "youtube"),
    (r"\bnote\s+pad\b", "notepad"),
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_decomposition_input(text: str) -> tuple[str, list[dict[str, str]]]:
    """Normalize user text and return typo-correction metadata."""
    normalized = _normalize_text(text)
    if not normalized:
        return "", []

    corrected = normalized
    corrections: list[dict[str, str]] = []
    for typo_pattern, replacement in _COMMON_TYPO_CORRECTIONS:
        regex = re.compile(typo_pattern, flags=re.IGNORECASE)
        for match in regex.finditer(corrected):
            found = match.group(0)
            corrections.append({"from": found, "to": replacement})
        corrected = regex.sub(replacement, corrected)
    return _normalize_text(corrected), corrections


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
    content, _ = normalize_decomposition_input(text)
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
    content, _ = normalize_decomposition_input(text)
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


def _extract_json_array_payload(raw_output: str) -> list[object]:
    text = str(raw_output or "").strip()
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    payload = text[start : end + 1] if start >= 0 and end > start else text
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_structured_steps(raw_output: str, max_steps: int) -> list[dict[str, str]]:
    parsed = _extract_json_array_payload(raw_output)
    if not parsed:
        return []
    bounded = _bounded_max_steps(max_steps)
    structured: list[dict[str, str]] = []
    for item in parsed:
        if isinstance(item, str):
            step = _clean_step_text(item)
            if not step:
                continue
            structured.append({"step": step, "tool": "unknown", "intent": "route required"})
        elif isinstance(item, dict):
            step = _clean_step_text(str(item.get("step", "")))
            tool = _clean_step_text(str(item.get("tool", ""))) or "unknown"
            intent = _clean_step_text(str(item.get("intent", ""))) or "route required"
            if step:
                structured.append({"step": step, "tool": tool, "intent": intent})
        if len(structured) >= bounded:
            break
    return structured


def decompose_task_structured(
    text: str,
    *,
    max_steps: int = MAX_STEPS,
    llm_decomposer: Callable[[str, int], str] | None = None,
    allow_llm_fallback: bool = True,
    route_predictor: Callable[[str], dict[str, object]] | None = None,
) -> list[dict[str, str]]:
    """Return decomposed steps enriched with tool and intent hints."""
    content, _ = normalize_decomposition_input(text)
    if not content:
        return []

    bounded_steps = _bounded_max_steps(max_steps)
    if allow_llm_fallback and llm_decomposer is not None:
        llm_output = ""
        try:
            llm_output = str(llm_decomposer(content, bounded_steps) or "").strip()
        except Exception:
            llm_output = ""
        parsed_structured = _parse_structured_steps(llm_output, max_steps=bounded_steps)
        if parsed_structured:
            return parsed_structured

    step_texts = decompose_task(
        content,
        max_steps=bounded_steps,
        llm_decomposer=llm_decomposer,
        allow_llm_fallback=allow_llm_fallback,
    )
    if not step_texts:
        return []

    predictor = route_predictor
    if predictor is None:
        from router import predict_route as predictor

    structured: list[dict[str, str]] = []
    for step in step_texts:
        route = predictor(step)
        tool = str(route.get("tool", "")).strip() or "unknown"
        intent = str(route.get("intent", "")).strip() or "route required"
        structured.append(
            {
                "step": step,
                "tool": tool,
                "intent": intent,
            }
        )
    return structured


__all__ = [
    "needs_decomposition",
    "decompose_task",
    "decompose_task_structured",
    "normalize_decomposition_input",
]
