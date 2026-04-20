"""Keyword-matched reusable task skills for VOCO prompt hints."""

from __future__ import annotations

import re
from typing import NamedTuple


class Skill(NamedTuple):
    name: str
    description: str
    trigger_keywords: tuple[str, ...]
    steps: tuple[dict, ...]
    notes: str = ""


SKILLS: tuple[Skill, ...] = (
    Skill(
        name="youtube_search_and_play",
        description="Open YouTube, search for query, click first video",
        trigger_keywords=("youtube", "search", "video"),
        steps=(
            {"tool": "browser_navigate", "args": {"url": "https://www.youtube.com"}},
            {"tool": "browser_type", "args": {"text": "{QUERY}", "submit": True}},
            {"tool": "browser_get_state", "args": {"max_elements": 40}},
            {"tool": "browser_click", "args": {"element_name": "{FIRST_VIDEO_FROM_STATE}", "occurrence": 1}},
        ),
        notes="Use exact element names from browser_get_state output.",
    ),
    Skill(
        name="open_app_generic",
        description="Open an installed app by name",
        trigger_keywords=("open",),
        steps=({"tool": "open_app", "args": {"app_name": "{APP_NAME}"}},),
    ),
    Skill(
        name="file_search_and_open",
        description="Search indexed files and read first hit",
        trigger_keywords=("find", "file"),
        steps=(
            {"tool": "search_file", "args": {"query": "{QUERY}", "limit": 5}},
            {"tool": "read_file", "args": {"path": "{FIRST_RESULT_PATH}"}},
        ),
    ),
)


def find_matching_skill(user_input: str) -> Skill | None:
    input_words = set(re.findall(r"\b\w+\b", str(user_input).lower()))
    best_skill: Skill | None = None
    best_score = 0.0
    for skill in SKILLS:
        trigger_words = set(skill.trigger_keywords)
        overlap = len(trigger_words & input_words)
        score = overlap / len(trigger_words) if trigger_words else 0.0
        if score >= 0.6 and score > best_score:
            best_score = score
            best_skill = skill
    return best_skill


def format_skill_hint(skill: Skill) -> str:
    step_lines = []
    for index, step in enumerate(skill.steps, start=1):
        step_lines.append(f"  {index}. {step['tool']}({step['args']})")
    if skill.notes:
        step_lines.append(f"  note: {skill.notes}")
    return f"SKILL HINT ({skill.name}):\n" + "\n".join(step_lines)
