"""Ollama client for VOCO JSON action-plan generation."""

import json

import requests

from constants import (
    OLLAMA_FAST_MODEL_CANDIDATES,
    OLLAMA_HEAVY_MODEL_CANDIDATES,
    OLLAMA_MODEL,
    OLLAMA_URL,
)


OLLAMA_CHAT_URL = f"{OLLAMA_URL}/api/chat"
_last_model_used = OLLAMA_MODEL


def _failure_plan(reason: str, failure_reason: str = "connection error") -> str:
    """Return a one-step report_failure plan JSON."""
    plan = [{"tool": "report_failure", "args": {"reason": reason}, "reason": failure_reason}]
    return json.dumps(plan, ensure_ascii=False)


def _fetch_available_models() -> list[str]:
    """Return available local Ollama model names."""
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        response.raise_for_status()
        models = [item.get("name", "") for item in response.json().get("models", [])]
        return [name for name in models if name]
    except Exception:
        return []


def _resolve_candidate(candidate: str, available_models: list[str]) -> str | None:
    """Resolve a preferred candidate to an installed model name."""
    for available in available_models:
        if available == candidate or available.startswith(f"{candidate}:"):
            return available
    return None


def _is_complex_task(user_message: str) -> bool:
    """Heuristic complexity estimate for dual-model routing."""
    text = user_message.lower()
    indicators = [
        " and ",
        " then ",
        " after ",
        " also ",
        " compare ",
        " analyze ",
        " explain ",
        " summarize ",
        " first ",
        " second ",
        " search ",
        " find ",
    ]
    return len(text.split()) > 12 or any(token in text for token in indicators)


def _candidate_chain(complex_task: bool) -> list[str]:
    if complex_task:
        return OLLAMA_HEAVY_MODEL_CANDIDATES + OLLAMA_FAST_MODEL_CANDIDATES
    return OLLAMA_FAST_MODEL_CANDIDATES + OLLAMA_HEAVY_MODEL_CANDIDATES


def _select_model(user_message: str, available_models: list[str]) -> str:
    """Pick the best installed model using fast/heavy preference."""
    chain = _candidate_chain(_is_complex_task(user_message))
    seen: set[str] = set()
    for candidate in chain:
        resolved = _resolve_candidate(candidate, available_models)
        if resolved and resolved not in seen:
            seen.add(resolved)
            return resolved
    return OLLAMA_MODEL


def _fallback_models(current_model: str, user_message: str, available_models: list[str]) -> list[str]:
    """Return smaller/alternative installed models after a failed attempt."""
    chain = _candidate_chain(_is_complex_task(user_message))
    resolved_models: list[str] = []
    for candidate in chain:
        resolved = _resolve_candidate(candidate, available_models)
        if resolved and resolved not in resolved_models:
            resolved_models.append(resolved)
    return [model for model in resolved_models if model != current_model]


def _extract_http_error_detail(response: requests.Response | None) -> str:
    """Extract a short human-readable error detail from an HTTP response."""
    if response is None:
        return "No response body"
    try:
        payload = response.json()
        if isinstance(payload, dict):
            if payload.get("error"):
                return str(payload["error"])
            if payload.get("message"):
                return str(payload["message"])
            return json.dumps(payload, ensure_ascii=False)[:240]
        return str(payload)[:240]
    except ValueError:
        text = response.text.strip()
        return text[:240] if text else "No error detail"


def _post_chat(messages: list[dict], model_name: str, temperature: float) -> requests.Response:
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_ctx": 8192,
        },
    }
    return requests.post(OLLAMA_CHAT_URL, json=payload, timeout=60)


def _generate_internal(messages: list[dict], user_message: str, temperature: float) -> str:
    """Shared model request path with automatic model fallback."""
    global _last_model_used
    available_models = _fetch_available_models()
    selected_model = _select_model(user_message, available_models)
    _last_model_used = selected_model

    try:
        response = _post_chat(messages=messages, model_name=selected_model, temperature=temperature)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except requests.exceptions.ConnectionError:
        return _failure_plan("LLM connection failed (Ollama unreachable).")
    except requests.exceptions.Timeout:
        return _failure_plan("LLM request timed out after 60s.")
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        detail = _extract_http_error_detail(exc.response)
        if status == 500 and "requires more system memory" in detail.lower():
            for fallback_model in _fallback_models(selected_model, user_message, available_models):
                try:
                    response = _post_chat(messages=messages, model_name=fallback_model, temperature=temperature)
                    response.raise_for_status()
                    _last_model_used = fallback_model
                    return response.json()["message"]["content"]
                except Exception:
                    continue
        return _failure_plan(f"LLM server returned HTTP {status}: {detail}")
    except Exception as exc:
        return _failure_plan(f"LLM unexpected error: {exc}")


def generate(system_prompt: str, user_message: str, temperature: float = 0.1) -> str:
    """Generate an action plan for a task."""
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    return _generate_internal(
        messages=payload["messages"],
        user_message=user_message,
        temperature=temperature,
    )


def generate_with_history(system_prompt: str, messages: list, temperature: float = 0.05) -> str:
    """
    Generate with explicit conversation history.
    Used for correction retries after formatting failures.
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    result = _generate_internal(
        messages=full_messages,
        user_message=messages[-1]["content"] if messages else "",
        temperature=temperature,
    )
    if "retry failed" not in result and "\"reason\": \"connection error\"" in result:
        return result.replace("\"reason\": \"connection error\"", "\"reason\": \"retry error\"")
    return result


def check_ollama_running() -> bool:
    """Return True if Ollama is reachable and at least one model is installed."""
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        response.raise_for_status()
        models = [model.get("name", "") for model in response.json().get("models", [])]
        return any(models)
    except Exception:
        return False


def get_last_model_used() -> str:
    """Return the model name used by the latest generate/generate_with_history call."""
    return _last_model_used
