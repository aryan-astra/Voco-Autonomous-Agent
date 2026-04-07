"""Ollama client for VOCO JSON action-plan generation."""

import json

import requests

from constants import OLLAMA_MODEL, OLLAMA_URL


OLLAMA_CHAT_URL = f"{OLLAMA_URL}/api/chat"


def _failure_plan(reason: str, failure_reason: str = "connection error") -> str:
    """Return a one-step report_failure plan JSON."""
    plan = [{"tool": "report_failure", "args": {"reason": reason}, "reason": failure_reason}]
    return json.dumps(plan, ensure_ascii=False)


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


def generate(system_prompt: str, user_message: str, temperature: float = 0.1) -> str:
    """
    Generate a plan using Ollama chat endpoint with deterministic settings.
    Returns raw model text.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_ctx": 8192,
        },
    }

    try:
        response = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return data["message"]["content"]
    except requests.exceptions.ConnectionError:
        return _failure_plan("LLM connection failed (Ollama unreachable).")
    except requests.exceptions.Timeout:
        return _failure_plan("LLM request timed out after 60s.")
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        detail = _extract_http_error_detail(exc.response)
        return _failure_plan(f"LLM server returned HTTP {status}: {detail}")
    except Exception as exc:
        return _failure_plan(f"LLM unexpected error: {exc}")


def generate_with_history(system_prompt: str, messages: list, temperature: float = 0.05) -> str:
    """
    Generate with explicit conversation history.
    Used for correction retries after formatting failures.
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": OLLAMA_MODEL,
        "messages": full_messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_ctx": 8192,
        },
    }

    try:
        response = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except requests.exceptions.ConnectionError:
        return _failure_plan("Retry failed: LLM connection failed.", "retry error")
    except requests.exceptions.Timeout:
        return _failure_plan("Retry failed: LLM request timed out after 60s.", "retry error")
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        detail = _extract_http_error_detail(exc.response)
        return _failure_plan(f"Retry failed: LLM HTTP {status}: {detail}", "retry error")
    except Exception as exc:
        return _failure_plan(f"Retry failed: unexpected LLM error: {exc}", "retry error")


def check_ollama_running() -> bool:
    """Return True if Ollama is reachable and target model is available."""
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if response.status_code != 200:
            return False
        models = [model["name"] for model in response.json().get("models", [])]
        return any(OLLAMA_MODEL in name for name in models)
    except Exception:
        return False
