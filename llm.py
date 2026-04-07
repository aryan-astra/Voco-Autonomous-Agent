"""Ollama client for VOCO JSON action-plan generation."""

import requests

from constants import OLLAMA_MODEL, OLLAMA_URL


OLLAMA_CHAT_URL = f"{OLLAMA_URL}/api/chat"


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
        return "[]"
    except requests.exceptions.Timeout:
        return "[]"
    except Exception as exc:
        return (
            "[{\"tool\": \"report_failure\", "
            f"\"args\": {{\"reason\": \"LLM error: {exc}\"}}, "
            "\"reason\": \"connection error\"}]"
        )


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
    except Exception as exc:
        return (
            "[{\"tool\": \"report_failure\", "
            f"\"args\": {{\"reason\": \"retry failed: {exc}\"}}, "
            "\"reason\": \"retry error\"}]"
        )


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
