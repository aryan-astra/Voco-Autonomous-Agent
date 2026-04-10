"""Lightweight tool-first router and deterministic argument extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer

    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False


INTENT_CATALOG: dict[str, dict] = {
    "open_file": {
        "tool": "read_file",
        "required_args": ("path",),
        "examples": [
            "open file notes.txt",
            "read file config.py",
            "show file logs/output.txt",
        ],
    },
    "list_files": {
        "tool": "list_files",
        "required_args": tuple(),
        "examples": [
            "list files",
            "show files in workspace",
            "show directory contents",
        ],
    },
    "open_app": {
        "tool": "open_app",
        "required_args": ("app_name",),
        "examples": [
            "open notepad",
            "open calculator",
            "launch chrome",
            "start spotify",
        ],
    },
    "browser_navigate": {
        "tool": "browser_navigate",
        "required_args": ("url",),
        "examples": [
            "open youtube",
            "go to chatgpt.com",
            "visit github.com",
            "open browser and go to google",
        ],
    },
    "browser_switch_profile": {
        "tool": "browser_switch_profile",
        "required_args": ("profile_mode",),
        "examples": [
            "switch chrome profile to default",
            "set browser profile to snapshot",
            "change edge profile to automation",
        ],
    },
    "browser_type": {
        "tool": "browser_type",
        "required_args": ("text",),
        "examples": [
            "type hello in browser",
            "search for mkbhd",
            "write hi and press enter",
        ],
    },
    "browser_click": {
        "tool": "browser_click",
        "required_args": ("element_name",),
        "examples": [
            "click first video",
            "click sign in button",
            "open latest result",
        ],
    },
    "browser_get_state": {
        "tool": "browser_get_state",
        "required_args": tuple(),
        "examples": [
            "copy the first 5 comments from this page",
            "read the video description",
            "extract content from the current website",
            "get comments from this tab",
        ],
    },
    "search_local_paths": {
        "tool": "search_local_paths",
        "required_args": ("query",),
        "examples": [
            "find AIOT-content folder on my pc",
            "search local file report.pdf",
            "locate folder in this pc",
        ],
    },
    "search_in_explorer": {
        "tool": "search_in_explorer",
        "required_args": ("query",),
        "examples": [
            "open file explorer and find AIOT-content folder",
            "search in explorer for project folder",
        ],
    },
    "write_in_notepad": {
        "tool": "write_in_notepad",
        "required_args": tuple(),
        "examples": [
            "open notepad and write hello",
            "type this in notepad",
            "paste text into notepad",
        ],
    },
    "save_text_to_desktop_file": {
        "tool": "save_text_to_desktop_file",
        "required_args": tuple(),
        "examples": [
            "save this text to desktop file",
            "save content in notes.txt on desktop",
        ],
    },
    "open_existing_document": {
        "tool": "open_existing_document",
        "required_args": ("extension",),
        "examples": [
            "open pdf file",
            "open existing ppt file",
            "open powerpoint file on my pc",
        ],
    },
    "youtube_comment_pipeline": {
        "tool": "youtube_comment_pipeline",
        "required_args": ("query",),
        "examples": [
            "open youtube and search mkbhd and save comments",
            "youtube comment pipeline for latest tech video",
        ],
    },
    "web_codegen_autofix": {
        "tool": "web_codegen_autofix",
        "required_args": ("request",),
        "examples": [
            "write a python file for odd even checker",
            "create .py file and run it",
            "generate code and fix runtime errors",
        ],
    },
}


_BROWSER_KEYWORDS = ("youtube", "video", "browser", "website", "web", "chatgpt", "x.com", "google")
_APP_ALIASES = (
    "notepad",
    "calculator",
    "chrome",
    "edge",
    "firefox",
    "spotify",
    "explorer",
    "powerpoint",
)
_PROFILE_MODES = ("default", "snapshot", "automation")
_OPEN_APP_GENERIC_TOKENS = {
    "app",
    "application",
    "program",
    "tool",
    "browser",
    "website",
    "web",
    "video",
    "content",
    "comment",
    "comments",
    "description",
    "it",
    "this",
    "that",
}
_OPEN_APP_VERB_REGEX = re.compile(r"\b(?:open|launch|start)\b", flags=re.IGNORECASE)
_BROWSER_STATE_VERB_REGEX = re.compile(r"\b(?:copy|read|extract|get)\b", flags=re.IGNORECASE)
_BROWSER_STATE_TARGET_REGEX = re.compile(r"\b(?:content|comments?|description)\b", flags=re.IGNORECASE)
_NOTEPAD_PASTE_REGEX = re.compile(r"\bpaste\b", flags=re.IGNORECASE)
_DESKTOP_SAVE_REFERENCE_REGEX = re.compile(r"\bsave\s+(?:it|this|that)\b", flags=re.IGNORECASE)
_DESKTOP_SAVE_FILE_REGEX = re.compile(
    r"\bsave(?:\s+(?:the|a|an|this|that|it))?\s+file\b",
    flags=re.IGNORECASE,
)
_NOTEPAD_CLIPBOARD_REFERENCES = {
    "it",
    "this",
    "that",
    "them",
    "those",
    "these",
    "content",
    "the content",
    "description",
    "the description",
    "comment",
    "comments",
    "the comment",
    "the comments",
}
_BROWSER_STATE_QUANTITY_REGEX = re.compile(
    r"\b(?:top|first|last|latest)?\s*(\d{1,3})\s+(?:comments?|results?|items?|elements?|entries?|lines?)\b",
    flags=re.IGNORECASE,
)
_BROWSER_STATE_DEFAULT_MAX_ELEMENTS = 40
_BROWSER_STATE_MAX_ELEMENTS = 120
_BROWSER_STATE_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_LOCAL_CONTENT_TERMS = (
    "file",
    "folder",
    "directory",
    "path",
    "desktop",
    "notepad",
    "document",
    "on my pc",
    "on this pc",
    "in my pc",
    "in this pc",
    "local",
)
_ACTION_VERBS = (
    "open",
    "search",
    "find",
    "locate",
    "switch",
    "set",
    "change",
    "go",
    "visit",
    "click",
    "type",
    "write",
    "paste",
    "save",
    "play",
    "pause",
    "extract",
    "copy",
    "read",
    "get",
    "run",
    "create",
    "generate",
    "focus",
    "press",
    "hit",
    "submit",
)
_ACTION_VERB_PATTERN = "|".join(re.escape(verb) for verb in _ACTION_VERBS)
_ACTION_VERB_REGEX = re.compile(rf"\b(?:{_ACTION_VERB_PATTERN})\b", flags=re.IGNORECASE)
_STEP_BOUNDARY_START_TOKENS = tuple(
    dict.fromkeys(
        _ACTION_VERBS
        + (
            "again",
            "reopen",
            "refocus",
            "repeat",
            "retry",
        )
    )
)
_STEP_BOUNDARY_START_PATTERN = "|".join(re.escape(token) for token in _STEP_BOUNDARY_START_TOKENS)
_HARD_STEP_SPLIT_REGEX = re.compile(r"\b(?:and then|then|after that|afterwards)\b|;", flags=re.IGNORECASE)
_SOFT_STEP_SPLIT_REGEX = re.compile(
    rf"(?:\s+\band\b\s+|\s*,\s*)(?=(?:please\s+)?(?:{_STEP_BOUNDARY_START_PATTERN})\b)",
    flags=re.IGNORECASE,
)
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


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    confidence: float
    tool: str
    args: dict[str, object]
    missing_args: list[str]
    rejected_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 4),
            "tool": self.tool,
            "args": self.args,
            "missing_args": self.missing_args,
            "rejected_reason": self.rejected_reason,
        }


def _extract_quoted_values(text: str) -> list[str]:
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', text)
    values: list[str] = []
    for left, right in quoted:
        value = (left or right).strip()
        if value:
            values.append(value)
    return values


def _extract_url(text: str) -> str | None:
    explicit = re.search(r"(https?://[^\s\"']+)", text, flags=re.IGNORECASE)
    if explicit:
        return explicit.group(1).rstrip(".,;:!?)")

    domain = re.search(r"\b([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+(?:/[^\s\"']*)?)", text, flags=re.IGNORECASE)
    if domain:
        value = domain.group(1).strip().rstrip(".,;:!?)")
        if not value.lower().startswith(("http://", "https://")):
            return f"https://{value}"
        return value

    if "youtube" in text.lower():
        return "https://www.youtube.com"
    if "chatgpt" in text.lower():
        return "https://chatgpt.com"
    if "google" in text.lower():
        return "https://www.google.com"
    return None


def _extract_path_token(text: str) -> str | None:
    windows_path = re.search(r"([a-zA-Z]:\\[^\"'\r\n]+)", text)
    if windows_path:
        return windows_path.group(1).strip().rstrip(".,;:!?")

    token_with_ext = re.search(r"\b([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]{1,10})\b", text)
    if token_with_ext:
        return token_with_ext.group(1).strip().rstrip(".,;:!?")
    return None


def _extract_filename(text: str, extension: str) -> str | None:
    match = re.search(rf"\b([a-zA-Z0-9_\-]+\.{re.escape(extension)})\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _extract_browser_from_text(text: str) -> str | None:
    lower = text.lower()
    if "chrome" in lower:
        return "chrome"
    if "edge" in lower:
        return "edge"
    if "firefox" in lower:
        return "firefox"
    return None


def _clean_phrase(raw: str) -> str:
    cleaned = str(raw).strip().strip("\"'").strip()
    cleaned = cleaned.rstrip(".,;:!?")
    return re.sub(r"\s+", " ", cleaned)


def _normalize_browser_text(raw: str) -> str:
    cleaned = str(raw or "").strip().strip("\"'").strip()
    cleaned = cleaned.rstrip(".,;:!?")
    if not cleaned:
        return ""
    converted = _BROWSER_NEWLINE_TOKEN_REGEX.sub("\n", cleaned)
    converted = re.sub(r"[ \t]*\n[ \t]*", "\n", converted)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in converted.splitlines()]
    return "\n".join(lines).strip()


def _browser_submit_requested(text: str, include_search: bool = False) -> bool:
    source = str(text or "")
    if _BROWSER_SUBMIT_REGEX.search(source):
        return True
    if include_search and re.search(r"\bsearch(?:\s+for)?\b", source, flags=re.IGNORECASE):
        return True
    return False


def _browser_multiline_requested(text: str) -> bool:
    return _BROWSER_MULTILINE_REGEX.search(str(text or "")) is not None


def _has_action_verb(text: str) -> bool:
    return _ACTION_VERB_REGEX.search(str(text or "")) is not None


def _split_candidate_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    hard_chunks = [chunk for chunk in re.split(_HARD_STEP_SPLIT_REGEX, text) if chunk and chunk.strip()]
    for chunk in hard_chunks:
        for candidate in re.split(_SOFT_STEP_SPLIT_REGEX, chunk):
            cleaned = _clean_phrase(candidate)
            if cleaned:
                clauses.append(cleaned)
    return clauses


def _extract_search_query(text: str) -> str | None:
    match = re.search(r"\bsearch(?:\s+for)?\s+(.+)$", text, flags=re.IGNORECASE)
    if match:
        return _clean_phrase(match.group(1))
    return None


def _extract_notepad_text(text: str) -> str | None:
    quoted = _extract_quoted_values(text)
    if quoted:
        return quoted[0]
    match = re.search(
        r"\b(?:write|type|paste)\s+(.+?)(?:\s+(?:in|into)\s+notepad|\s*$)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_phrase(match.group(1))
    return None


def _extract_local_search_query(text: str) -> str | None:
    pattern = r"\b(?:search|find|locate)\s+(?:for\s+)?(.+?)(?:\s+(?:on|in)\s+(?:my|this)\s+(?:pc|computer)|\s*$)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return _clean_phrase(match.group(1))
    trailing = re.search(r"\b(?:search|find|locate)\s+(?:for\s+)?(.+)$", text, flags=re.IGNORECASE)
    if trailing:
        return _clean_phrase(trailing.group(1))
    return None


def _normalize_open_app_candidate(raw: str) -> str:
    normalized = _clean_phrase(raw).lower()
    normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    return normalized.strip()


def _is_credible_open_app_token(raw: str) -> bool:
    candidate = _normalize_open_app_candidate(raw)
    if not candidate:
        return False
    if candidate in _OPEN_APP_GENERIC_TOKENS:
        return False
    parts = [part for part in re.split(r"[\s\-]+", candidate) if part]
    if not parts:
        return False
    return any(part not in _OPEN_APP_GENERIC_TOKENS for part in parts)


def _extract_open_app_name(text: str) -> str | None:
    lower = text.lower()
    for app in _APP_ALIASES:
        if re.search(rf"\b{re.escape(app)}\b", lower):
            return app
    match = re.search(r"\b(?:open|launch|start)\s+([a-z0-9][a-z0-9 \-]{1,40})$", text, flags=re.IGNORECASE)
    if match:
        candidate = _normalize_open_app_candidate(match.group(1))
        if _is_credible_open_app_token(candidate):
            return candidate
    return None


def _has_credible_open_app_signal(text: str, args: dict[str, object]) -> bool:
    source = str(text or "")
    if _OPEN_APP_VERB_REGEX.search(source) is None:
        return False
    app_name = str(args.get("app_name", "")).strip()
    if _is_credible_open_app_token(app_name):
        return True
    extracted = _extract_open_app_name(source)
    return bool(extracted and _is_credible_open_app_token(extracted))


def _looks_like_browser_state_read_request(text: str) -> bool:
    source = str(text or "")
    lower = source.lower()
    if _BROWSER_STATE_VERB_REGEX.search(source) is None:
        return False
    if _BROWSER_STATE_TARGET_REGEX.search(source) is None:
        return False

    has_browser_signal = (
        any(token in lower for token in _BROWSER_KEYWORDS)
        or _extract_url(source) is not None
        or re.search(r"\b(?:page|tab|site|web|video)\b", source, flags=re.IGNORECASE) is not None
    )

    if any(token in lower for token in _LOCAL_CONTENT_TERMS) and not has_browser_signal:
        return False

    if has_browser_signal:
        return True
    return re.search(r"\b(?:comments?|description)\b", source, flags=re.IGNORECASE) is not None


def _coerce_browser_state_max_elements(value: int) -> int:
    return max(1, min(_BROWSER_STATE_MAX_ELEMENTS, int(value)))


def _extract_browser_state_max_elements(text: str) -> int:
    source = str(text or "")
    lower = source.lower()

    quantity_match = _BROWSER_STATE_QUANTITY_REGEX.search(source)
    if quantity_match:
        return _coerce_browser_state_max_elements(int(quantity_match.group(1)))

    verb_number_match = re.search(r"\b(?:copy|read|extract|get)\s+(?:the\s+)?(\d{1,3})\b", source, flags=re.IGNORECASE)
    if verb_number_match:
        return _coerce_browser_state_max_elements(int(verb_number_match.group(1)))

    for word, value in _BROWSER_STATE_NUMBER_WORDS.items():
        if re.search(
            rf"\b(?:top|first|last|latest)?\s*{re.escape(word)}\s+(?:comments?|results?|items?|elements?|entries?|lines?)\b",
            lower,
        ):
            return _coerce_browser_state_max_elements(value)

    return _BROWSER_STATE_DEFAULT_MAX_ELEMENTS


def extract_args(intent: str, text: str) -> dict[str, object]:
    task = str(text or "")
    lower = task.lower()
    args: dict[str, object] = {}

    if intent == "open_file":
        path = _extract_path_token(task)
        if path:
            args["path"] = path
        return args

    if intent == "list_files":
        quoted = _extract_quoted_values(task)
        if quoted:
            args["directory"] = quoted[0]
        return args

    if intent == "open_app":
        app_name = _extract_open_app_name(task)
        if app_name:
            args["app_name"] = app_name
        return args

    if intent == "browser_navigate":
        url = _extract_url(task)
        if url:
            args["url"] = url
        browser = _extract_browser_from_text(task)
        if browser:
            args["browser"] = browser
        return args

    if intent == "browser_switch_profile":
        for mode in _PROFILE_MODES:
            if re.search(rf"\b{mode}\b", lower):
                args["profile_mode"] = mode
                break
        browser = _extract_browser_from_text(task)
        if browser:
            args["browser"] = browser
        args["relaunch"] = True
        return args

    if intent == "browser_type":
        query = _extract_search_query(task)
        if query:
            normalized_query = _normalize_browser_text(query)
            if normalized_query:
                args["text"] = normalized_query
                args["element_name"] = "search"
                args["submit"] = True
            return args

        typed_source: str | None = None
        quoted = _extract_quoted_values(task)
        if quoted:
            typed_source = quoted[0]
        fallback = re.search(
            r"\b(?:type|write|paste)\s+(.+?)(?=\s+(?:and|then)\s+(?:(?:press|hit)\s+enter|send|submit)\b|\s*$)",
            task,
            flags=re.IGNORECASE,
        )
        if fallback:
            typed_source = fallback.group(1)
        if typed_source:
            typed_text = _normalize_browser_text(typed_source)
            if typed_text:
                args["text"] = typed_text
                if _browser_multiline_requested(task) or "\n" in typed_text:
                    args["multiline"] = True
                    args["newline_mode"] = "shift_enter"
                if _browser_submit_requested(task):
                    args["submit"] = True
        return args

    if intent == "browser_click":
        match = re.search(r"\b(?:click|open|play)\s+(?:the\s+)?(.+)$", task, flags=re.IGNORECASE)
        if match:
            args["element_name"] = _clean_phrase(match.group(1))
        return args

    if intent == "browser_get_state":
        state_limit = _extract_browser_state_max_elements(task)
        args["max_elements"] = max(20, min(120, state_limit * 4))
        args["text_limit"] = state_limit
        target_match = _BROWSER_STATE_TARGET_REGEX.search(task)
        if target_match:
            args["text_query"] = target_match.group(0)
        if re.search(r"\bcopy\b", lower):
            args["copy_to_clipboard"] = True
        return args

    if intent in {"search_local_paths", "search_in_explorer"}:
        query = _extract_local_search_query(task)
        if query:
            args["query"] = query
        if intent == "search_local_paths":
            if any(token in lower for token in ("folder", "directory")):
                args["kind"] = "folder"
            else:
                args["kind"] = "all"
            args["open_first"] = any(token in lower for token in ("open", "go to", "navigate"))
        if intent == "search_in_explorer":
            args["folders_only"] = True
        return args

    if intent == "write_in_notepad":
        content = _extract_notepad_text(task)
        paste_request = bool(_NOTEPAD_PASTE_REGEX.search(task) and "notepad" in lower)
        if content and content.lower() not in {"in a notepad file", "in notepad", "into notepad"}:
            normalized_content = content.lower()
            if paste_request and normalized_content in _NOTEPAD_CLIPBOARD_REFERENCES:
                args["paste_clipboard"] = True
            else:
                args["text"] = content
        if paste_request and "text" not in args:
            args["paste_clipboard"] = True
        return args

    if intent == "save_text_to_desktop_file":
        quoted = _extract_quoted_values(task)
        if quoted:
            args["content"] = quoted[0]
        has_explicit_content = bool(str(args.get("content", "")).strip())
        desktop_save_reference = bool(
            _DESKTOP_SAVE_REFERENCE_REGEX.search(lower)
            or _DESKTOP_SAVE_FILE_REGEX.search(lower)
            or re.search(r"\bsave\b.*\bdesktop\b", lower)
        )
        if "desktop" in lower and desktop_save_reference and not has_explicit_content:
            args["from_clipboard"] = True
        file_name = _extract_filename(task, "txt")
        if file_name:
            args["filename"] = file_name
        if "notepad" in lower:
            args["open_in_notepad"] = True
        return args

    if intent == "open_existing_document":
        if "pdf" in lower:
            args["extension"] = ".pdf"
        elif "pptx" in lower or "powerpoint" in lower or re.search(r"\bppt\b", lower):
            args["extension"] = ".pptx"
        return args

    if intent == "youtube_comment_pipeline":
        query = _extract_search_query(task)
        if not query:
            query = "latest technology video"
        args["query"] = query
        output_name = _extract_filename(task, "txt")
        if output_name:
            args["output_filename"] = output_name
        if "notepad" in lower:
            args["open_in_notepad"] = True
        return args

    if intent == "web_codegen_autofix":
        args["request"] = _clean_phrase(task)
        filename = re.search(r"\b([a-zA-Z0-9_\-]+\.py)\b", task)
        if filename:
            args["filename"] = filename.group(1)
        return args

    return args


def _guardrail_rejection(intent: str, text: str, args: dict[str, object]) -> str:
    lower = str(text or "").lower()

    if intent == "search_local_paths":
        query = str(args.get("query", "")).lower()
        if any(token in query for token in _BROWSER_KEYWORDS):
            return "browser_terms_detected_for_local_file_search"
        if any(token in lower for token in ("youtube", "video")) and "on my pc" not in lower:
            return "browser_media_request_should_not_route_to_local_search"

    if intent == "open_app":
        if not _has_credible_open_app_signal(text, args):
            return "open_app_requires_open_verb_and_app_signal"
        app_name = _normalize_open_app_candidate(str(args.get("app_name", "")))
        if app_name in {"youtube", "chatgpt", "google"}:
            return "web_destination_should_use_browser_navigation"

    if intent == "write_in_notepad":
        has_text = bool(str(args.get("text", "")).strip())
        paste_clipboard = bool(args.get("paste_clipboard"))
        if not has_text and not paste_clipboard:
            return "write_in_notepad_requires_text_or_clipboard_paste"

    if intent == "save_text_to_desktop_file":
        has_content = bool(str(args.get("content", "")).strip())
        from_clipboard = bool(args.get("from_clipboard"))
        if not has_content and not from_clipboard:
            return "desktop_save_requires_content_or_clipboard_source"

    return ""


class IntentRouter:
    """Predict intent and extract deterministic args without LLM dependency."""

    def __init__(self, catalog: dict[str, dict]) -> None:
        self._catalog = catalog
        self._labels: list[str] = []
        self._vectorizer = None
        self._classifier = None
        self._fallback_keywords: dict[str, set[str]] = {}
        self._train()

    def _train(self) -> None:
        texts: list[str] = []
        labels: list[str] = []
        fallback_keywords: dict[str, set[str]] = {}

        for intent, spec in self._catalog.items():
            examples = [str(item).strip().lower() for item in spec.get("examples", []) if str(item).strip()]
            if not examples:
                continue
            for sample in examples:
                texts.append(sample)
                labels.append(intent)
                tokens = set(re.findall(r"[a-z0-9_.:/\\-]+", sample))
                fallback_keywords.setdefault(intent, set()).update(tokens)

        self._labels = sorted(set(labels))
        self._fallback_keywords = fallback_keywords

        if not SKLEARN_AVAILABLE or not texts or len(self._labels) < 2:
            return

        try:
            vectorizer = TfidfVectorizer(ngram_range=(1, 2), lowercase=True)
            matrix = vectorizer.fit_transform(texts)
            classifier = RandomForestClassifier(
                n_estimators=120,
                random_state=42,
                class_weight="balanced",
                min_samples_leaf=1,
            )
            classifier.fit(matrix, labels)
            self._vectorizer = vectorizer
            self._classifier = classifier
        except Exception:
            self._vectorizer = None
            self._classifier = None

    def _predict_with_ml(self, text: str) -> tuple[str, float] | None:
        if self._vectorizer is None or self._classifier is None:
            return None
        try:
            matrix = self._vectorizer.transform([text.lower()])
            probabilities = self._classifier.predict_proba(matrix)[0]
            best_index = int(probabilities.argmax())
            labels = list(self._classifier.classes_)
            return str(labels[best_index]), float(probabilities[best_index])
        except Exception:
            return None

    def _predict_with_keywords(self, text: str) -> tuple[str, float]:
        tokens = set(re.findall(r"[a-z0-9_.:/\\-]+", text.lower()))
        best_intent = ""
        best_score = 0.0
        for intent, keywords in self._fallback_keywords.items():
            if not keywords:
                continue
            overlap = len(tokens & keywords)
            score = overlap / max(len(keywords), 1)
            if score > best_score:
                best_score = score
                best_intent = intent
        if not best_intent:
            return "unknown", 0.0
        return best_intent, min(0.79, 0.35 + best_score)

    def predict(self, text: str) -> RouteDecision:
        content = str(text or "").strip()
        if not content:
            return RouteDecision(
                intent="unknown",
                confidence=0.0,
                tool="",
                args={},
                missing_args=[],
                rejected_reason="empty_input",
            )

        lowered = content.lower()
        if re.match(r"^\s*search(?:\s+for)?\s+", content, flags=re.IGNORECASE):
            if "explorer" in lowered or "file explorer" in lowered:
                intent = "search_in_explorer"
                confidence = 0.93
            elif any(
                token in lowered
                for token in (
                    "on my pc",
                    "on this pc",
                    "in my pc",
                    "in this pc",
                    "local file",
                    "local folder",
                    "file on pc",
                    "folder on pc",
                    "directory",
                    "folder",
                    "path",
                )
            ):
                intent = "search_local_paths"
                confidence = 0.92
            else:
                intent = "browser_type"
                confidence = 0.94

            spec = self._catalog.get(intent, {})
            tool = str(spec.get("tool", "")).strip()
            args = extract_args(intent, content)
            required = [str(arg) for arg in spec.get("required_args", ())]
            missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
            rejected_reason = _guardrail_rejection(intent, content, args)
            final_confidence = confidence if not rejected_reason else 0.0
            return RouteDecision(
                intent=intent,
                confidence=max(0.0, min(1.0, float(final_confidence))),
                tool=tool,
                args=args,
                missing_args=missing_args,
                rejected_reason=rejected_reason,
            )

        if "notepad" in lowered and _NOTEPAD_PASTE_REGEX.search(content):
            intent = "write_in_notepad"
            confidence = 0.92
            spec = self._catalog.get(intent, {})
            tool = str(spec.get("tool", "")).strip()
            args = extract_args(intent, content)
            required = [str(arg) for arg in spec.get("required_args", ())]
            missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
            rejected_reason = _guardrail_rejection(intent, content, args)
            final_confidence = confidence if not rejected_reason else 0.0
            return RouteDecision(
                intent=intent,
                confidence=max(0.0, min(1.0, float(final_confidence))),
                tool=tool,
                args=args,
                missing_args=missing_args,
                rejected_reason=rejected_reason,
            )

        if "desktop" in lowered and _DESKTOP_SAVE_REFERENCE_REGEX.search(lowered):
            intent = "save_text_to_desktop_file"
            confidence = 0.9
            spec = self._catalog.get(intent, {})
            tool = str(spec.get("tool", "")).strip()
            args = extract_args(intent, content)
            required = [str(arg) for arg in spec.get("required_args", ())]
            missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
            rejected_reason = _guardrail_rejection(intent, content, args)
            final_confidence = confidence if not rejected_reason else 0.0
            return RouteDecision(
                intent=intent,
                confidence=max(0.0, min(1.0, float(final_confidence))),
                tool=tool,
                args=args,
                missing_args=missing_args,
                rejected_reason=rejected_reason,
            )

        if (
            re.search(r"\b(?:play|click|open)\b", lowered)
            and any(token in lowered for token in ("video", "result", "1st", "first", "latest"))
            and not any(token in lowered for token in ("file", "folder", "directory", "path", "on my pc", "on this pc"))
        ):
            intent = "browser_click"
            confidence = 0.91
            spec = self._catalog.get(intent, {})
            tool = str(spec.get("tool", "")).strip()
            args = extract_args(intent, content)
            required = [str(arg) for arg in spec.get("required_args", ())]
            missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
            rejected_reason = _guardrail_rejection(intent, content, args)
            final_confidence = confidence if not rejected_reason else 0.0
            return RouteDecision(
                intent=intent,
                confidence=max(0.0, min(1.0, float(final_confidence))),
                tool=tool,
                args=args,
                missing_args=missing_args,
                rejected_reason=rejected_reason,
            )

        if _looks_like_browser_state_read_request(content):
            intent = "browser_get_state"
            confidence = 0.9
            spec = self._catalog.get(intent, {})
            tool = str(spec.get("tool", "")).strip()
            args = extract_args(intent, content)
            required = [str(arg) for arg in spec.get("required_args", ())]
            missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
            rejected_reason = _guardrail_rejection(intent, content, args)
            final_confidence = confidence if not rejected_reason else 0.0
            return RouteDecision(
                intent=intent,
                confidence=max(0.0, min(1.0, float(final_confidence))),
                tool=tool,
                args=args,
                missing_args=missing_args,
                rejected_reason=rejected_reason,
            )

        ml_prediction = self._predict_with_ml(content)
        if ml_prediction is not None:
            intent, confidence = ml_prediction
        else:
            intent, confidence = self._predict_with_keywords(content)

        if intent not in self._catalog:
            return RouteDecision(
                intent="unknown",
                confidence=0.0,
                tool="",
                args={},
                missing_args=[],
                rejected_reason="no_supported_intent",
            )

        spec = self._catalog[intent]
        tool = str(spec.get("tool", "")).strip()
        args = extract_args(intent, content)
        required = [str(arg) for arg in spec.get("required_args", ())]
        missing_args = [name for name in required if name not in args or str(args.get(name, "")).strip() == ""]
        rejected_reason = _guardrail_rejection(intent, content, args)
        final_confidence = confidence if not rejected_reason else 0.0

        return RouteDecision(
            intent=intent,
            confidence=max(0.0, min(1.0, float(final_confidence))),
            tool=tool,
            args=args,
            missing_args=missing_args,
            rejected_reason=rejected_reason,
        )


ROUTER = IntentRouter(INTENT_CATALOG)


def predict_route(text: str) -> dict[str, object]:
    """Return normalized route decision for one atomic step."""
    return ROUTER.predict(text).to_dict()


def should_split_task(text: str) -> bool:
    """Return True for non-trivial requests that should be decomposed first."""
    content = str(text or "").strip()
    if not content:
        return False
    token_count = len(re.findall(r"[a-z0-9_.:/\\-]+", content.lower()))
    connectors = re.search(r"\b(?:and then|then|after that|afterwards|and)\b", content, flags=re.IGNORECASE)
    action_count = len(_ACTION_VERB_REGEX.findall(content))
    return token_count >= 9 or bool(connectors) or action_count >= 2


def split_task_into_steps(text: str) -> list[str]:
    """Split non-trivial user text into short atomic steps."""
    content = str(text or "").strip()
    if not content:
        return []
    if not should_split_task(content):
        return [content]

    normalized = re.sub(r"\s+", " ", content).strip()
    raw_steps = _split_candidate_clauses(normalized)
    steps: list[str] = []
    pending_prefix = ""
    for item in raw_steps:
        step = _clean_phrase(item)
        if not step:
            continue
        if _has_action_verb(step):
            if pending_prefix:
                step = _clean_phrase(f"{pending_prefix} {step}")
                pending_prefix = ""
            steps.append(step)
            continue
        if steps:
            steps[-1] = _clean_phrase(f"{steps[-1]} {step}")
        elif pending_prefix:
            pending_prefix = _clean_phrase(f"{pending_prefix} {step}")
        else:
            pending_prefix = step

    if pending_prefix and steps:
        steps[0] = _clean_phrase(f"{pending_prefix} {steps[0]}")

    steps = [step for step in steps if _has_action_verb(step) and len(step.split()) >= 2]

    if not steps:
        return [content]
    return steps
