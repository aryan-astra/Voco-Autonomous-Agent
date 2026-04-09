"""Ollama client for VOCO JSON action-plan generation."""

import json
import time

import requests

from constants import (
    OLLAMA_CTX_FALLBACK_LEVELS,
    OLLAMA_FAST_MODEL_CANDIDATES,
    OLLAMA_HEAVY_MODEL_CANDIDATES,
    OLLAMA_MODEL,
    OLLAMA_CONVERSATION_TIMEOUT_SECONDS,
    OLLAMA_NUM_CTX_CONVERSATION,
    OLLAMA_NUM_CTX_COMPLEX,
    OLLAMA_NUM_CTX_MIN,
    OLLAMA_NUM_CTX_SIMPLE,
    OLLAMA_REQUEST_TIMEOUT_SECONDS,
    OLLAMA_URL,
)


OLLAMA_CHAT_URL = f"{OLLAMA_URL}/api/chat"
_last_model_used = OLLAMA_MODEL
_last_num_ctx_used = OLLAMA_NUM_CTX_SIMPLE
_model_cache: list[str] = []
_model_cache_expires_at = 0.0
_MODEL_CACHE_TTL_SECONDS = 5


def _failure_plan(reason: str, failure_reason: str = "connection error") -> str:
    """Return a one-step report_failure plan JSON."""
    plan = [{"tool": "report_failure", "args": {"reason": reason}, "reason": failure_reason}]
    return json.dumps(plan, ensure_ascii=False)


def _fetch_available_models() -> list[str]:
    """Return available local Ollama model names."""
    global _model_cache, _model_cache_expires_at
    now = time.time()
    if _model_cache and now < _model_cache_expires_at:
        return list(_model_cache)
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        response.raise_for_status()
        models = [item.get("name", "") for item in response.json().get("models", [])]
        filtered = [name for name in models if name]
        _model_cache = filtered
        _model_cache_expires_at = now + _MODEL_CACHE_TTL_SECONDS
        return list(filtered)
    except Exception:
        if _model_cache:
            return list(_model_cache)
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
        " then ",
        " after that ",
        " step by step ",
        " one by one ",
        " compare ",
        " analyze ",
        " explain ",
        " summarize ",
        " first ",
        " second ",
        " search ",
        " find ",
        " why ",
    ]
    return len(text.split()) > 18 or any(token in text for token in indicators)


def _initial_num_ctx(user_message: str) -> int:
    """Choose initial context window by task complexity."""
    return OLLAMA_NUM_CTX_COMPLEX if _is_complex_task(user_message) else OLLAMA_NUM_CTX_SIMPLE


def _ctx_fallback_chain(initial_ctx: int) -> list[int]:
    """Return a descending list of context sizes to fit lower-memory devices."""
    chain = [initial_ctx]
    for ctx in OLLAMA_CTX_FALLBACK_LEVELS:
        if ctx < initial_ctx and ctx >= OLLAMA_NUM_CTX_MIN and ctx not in chain:
            chain.append(ctx)
    if OLLAMA_NUM_CTX_MIN not in chain:
        chain.append(OLLAMA_NUM_CTX_MIN)
    return chain


def _candidate_chain(complex_task: bool, prefer_fast: bool = False) -> list[str]:
    chain: list[str]
    if prefer_fast:
        chain = OLLAMA_FAST_MODEL_CANDIDATES + OLLAMA_HEAVY_MODEL_CANDIDATES
    elif complex_task:
        chain = OLLAMA_HEAVY_MODEL_CANDIDATES + OLLAMA_FAST_MODEL_CANDIDATES
    else:
        chain = OLLAMA_FAST_MODEL_CANDIDATES + OLLAMA_HEAVY_MODEL_CANDIDATES
    return list(dict.fromkeys(chain))


def _select_model(user_message: str, available_models: list[str], prefer_fast: bool = False) -> str:
    """Pick the best installed model using fast/heavy preference."""
    chain = _candidate_chain(_is_complex_task(user_message), prefer_fast=prefer_fast)
    seen: set[str] = set()
    for candidate in chain:
        resolved = _resolve_candidate(candidate, available_models)
        if resolved and resolved not in seen:
            seen.add(resolved)
            return resolved
    return OLLAMA_MODEL


def _fallback_models(
    current_model: str,
    user_message: str,
    available_models: list[str],
    prefer_fast: bool = False,
) -> list[str]:
    """Return smaller/alternative installed models after a failed attempt."""
    chain = _candidate_chain(_is_complex_task(user_message), prefer_fast=prefer_fast)
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


def _post_chat(
    messages: list[dict],
    model_name: str,
    temperature: float,
    num_ctx: int,
    timeout_seconds: int,
    num_predict: int | None = None,
) -> requests.Response:
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_ctx": num_ctx,
        },
    }
    if num_predict is not None:
        payload["options"]["num_predict"] = num_predict
    return requests.post(OLLAMA_CHAT_URL, json=payload, timeout=timeout_seconds)


def _generate_result_internal(
    messages: list[dict],
    user_message: str,
    temperature: float,
    *,
    prefer_fast: bool = False,
    preferred_ctx: int | None = None,
    timeout_seconds: int = OLLAMA_REQUEST_TIMEOUT_SECONDS,
    num_predict: int | None = None,
) -> tuple[bool, str]:
    """Shared model request path with automatic model/context fallback."""
    global _last_model_used, _last_num_ctx_used
    available_models = _fetch_available_models()
    selected_model = _select_model(user_message, available_models, prefer_fast=prefer_fast)
    _last_model_used = selected_model
    initial_ctx = preferred_ctx if preferred_ctx is not None else _initial_num_ctx(user_message)
    primary_ctx_chain = _ctx_fallback_chain(initial_ctx)
    last_http_detail = ""

    for ctx in primary_ctx_chain:
        try:
            _last_num_ctx_used = ctx
            response = _post_chat(
                messages=messages,
                model_name=selected_model,
                temperature=temperature,
                num_ctx=ctx,
                timeout_seconds=timeout_seconds,
                num_predict=num_predict,
            )
            response.raise_for_status()
            return True, response.json()["message"]["content"]
        except requests.exceptions.ConnectionError:
            return False, "LLM connection failed (Ollama unreachable)."
        except requests.exceptions.Timeout:
            return False, f"LLM request timed out after {timeout_seconds}s."
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            detail = _extract_http_error_detail(exc.response)
            last_http_detail = f"LLM server returned HTTP {status}: {detail}"
            if status == 500 and "requires more system memory" in detail.lower():
                # keep trying with smaller context for same model
                continue
            return False, last_http_detail
        except Exception as exc:
            return False, f"LLM unexpected error: {exc}"

    # If primary model still fails under lowest context, try alternate installed models.
    for fallback_model in _fallback_models(
        selected_model,
        user_message,
        available_models,
        prefer_fast=prefer_fast,
    ):
        for ctx in primary_ctx_chain:
            try:
                _last_num_ctx_used = ctx
                response = _post_chat(
                    messages=messages,
                    model_name=fallback_model,
                    temperature=temperature,
                    num_ctx=ctx,
                    timeout_seconds=timeout_seconds,
                    num_predict=num_predict,
                )
                response.raise_for_status()
                _last_model_used = fallback_model
                return True, response.json()["message"]["content"]
            except requests.exceptions.Timeout:
                last_http_detail = f"LLM request timed out after {timeout_seconds}s."
                break
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                detail = _extract_http_error_detail(exc.response)
                last_http_detail = f"LLM server returned HTTP {status}: {detail}"
                if status == 500 and "requires more system memory" in detail.lower():
                    continue
                break
            except Exception:
                break

    if last_http_detail:
        return False, last_http_detail
    return False, "LLM request failed after adaptive model/context fallback."


def _generate_internal(messages: list[dict], user_message: str, temperature: float) -> str:
    """Generate a JSON plan response, converting transport errors into report_failure plans."""
    success, content = _generate_result_internal(
        messages=messages,
        user_message=user_message,
        temperature=temperature,
    )
    if success:
        return content
    return _failure_plan(content)


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


def generate_conversation(user_message: str, temperature: float = 0.2) -> tuple[bool, str]:
    """Generate a model-based conversational response (no JSON planning)."""
    system_prompt = (
        "You are VOCO, a local desktop assistant. "
        "Reply naturally in plain text and keep responses concise."
    )
    success, content = _generate_result_internal(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        user_message=user_message,
        temperature=temperature,
        prefer_fast=True,
        preferred_ctx=OLLAMA_NUM_CTX_CONVERSATION,
        timeout_seconds=OLLAMA_CONVERSATION_TIMEOUT_SECONDS,
        num_predict=160,
    )
    if success and not content.strip():
        return False, "LLM returned an empty response."
    return success, content.strip()


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
    """Return True if Ollama is reachable and the configured model is installed."""
    models = _fetch_available_models()
    return _resolve_candidate(OLLAMA_MODEL, models) is not None


def get_last_model_used() -> str:
    """Return the model name used by the latest generate/generate_with_history call."""
    return _last_model_used


def get_last_num_ctx_used() -> int:
    """Return the num_ctx value used by the latest generate/generate_with_history call."""
    return _last_num_ctx_used
