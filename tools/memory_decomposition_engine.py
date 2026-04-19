"""Memory-efficient task decomposition engine for constrained CPU-only systems."""

from __future__ import annotations

import argparse
import functools
import gc
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import requests


PLANNER_PROMPT_TEMPLATE = """You are TaskDecomposer v3. Your ONLY job is to break high-level goals into atomic, executable steps.
RULES:
Output format: JSON array of strings ONLY. Example: ["step1", "step2"]
Each step must be < 20 words, imperative verb first, no explanations.
Steps must be sequential and independent (no "if/else" logic).
If goal is unclear, output: ["Ask user to clarify: {{specific question}}"]
GOAL: "{user_goal}"
OUTPUT (JSON array only):"""

EXECUTOR_PROMPT_TEMPLATE = """You are StepExecutor v2. Execute ONE atomic step.
CONTEXT:
Overall goal: {goal_summary}
Previous step result: {previous_step_result}
Recent context: {recent_context}
Current step: "{current_step}"
RULES:
If step requires code: output ONLY valid Python/bash code block, no explanations.
If step requires reasoning: output answer in < 3 sentences.
If step is ambiguous: output "AMBIGUOUS: {{clarifying question}}"
NEVER output markdown, NEVER explain your thinking.
OUTPUT:"""

SYNTHESIZER_PROMPT_TEMPLATE = """You are ResultSynthesizer v1. Create a 3-sentence summary from completed steps.
COMPLETED STEPS:
{list_of_step_results}
RULES:
Sentence 1: What was the original goal?
Sentence 2: What was the key outcome?
Sentence 3: What should the user do next (if anything)?
NO bullet points, NO markdown, NO extra text.
OUTPUT (3 sentences only):"""

InferenceFunction = Callable[..., str]
FallbackPromptBuilder = Callable[[str, int, Exception], str]

_CODE_BLOCK_REGEX = re.compile(r"```(?P<lang>[a-zA-Z0-9_-]+)?\s*(?P<code>.*?)```", flags=re.DOTALL)
_NUMBERED_LINE_REGEX = re.compile(r"^\s*\d+\.\s*(.+?)\s*$", flags=re.MULTILINE)


def _utc_iso_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(tz=UTC).isoformat()


def _configure_logger(log_path: Path) -> logging.Logger:
    """Create a file logger scoped to this engine instance."""
    logger = logging.getLogger(f"memory_decomposition_engine::{log_path}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _extract_json_array_fragment(raw_text: str) -> str:
    """Extract the first JSON array-like slice from model output."""
    text = str(raw_text or "").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _clean_step_text(step: str, max_words: int = 20) -> str:
    """Normalize a planner step and enforce a soft word limit."""
    normalized = re.sub(r"\s+", " ", str(step or "")).strip().strip("\"'")
    if not normalized:
        return ""
    words = normalized.split()
    if len(words) > max_words:
        normalized = " ".join(words[:max_words])
    return normalized


def _parse_step_list(raw_text: str, max_steps: int = 12) -> list[str]:
    """Parse planner output as JSON array, then fallback to numbered lines."""
    fragment = _extract_json_array_fragment(raw_text)
    parsed_steps: list[str] = []
    try:
        decoded = json.loads(fragment)
        if isinstance(decoded, list):
            for item in decoded:
                if isinstance(item, str):
                    cleaned = _clean_step_text(item)
                    if cleaned:
                        parsed_steps.append(cleaned)
    except json.JSONDecodeError:
        parsed_steps = []

    if not parsed_steps:
        numbered = [match.group(1).strip() for match in _NUMBERED_LINE_REGEX.finditer(str(raw_text or ""))]
        parsed_steps = [_clean_step_text(step) for step in numbered if _clean_step_text(step)]

    deduped: list[str] = []
    seen: set[str] = set()
    for step in parsed_steps:
        key = step.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(step)
        if len(deduped) >= max_steps:
            break
    return deduped


def _parse_executor_response(raw_text: str) -> dict[str, str]:
    """Parse executor output into one of: code, ambiguous, or answer."""
    text = str(raw_text or "").strip()
    if not text:
        return {"kind": "answer", "value": ""}

    if text.upper().startswith("AMBIGUOUS:"):
        question = text.split(":", 1)[1].strip() if ":" in text else text
        return {"kind": "ambiguous", "value": question}

    code_match = _CODE_BLOCK_REGEX.search(text)
    if code_match:
        language = (code_match.group("lang") or "").strip().lower()
        code = code_match.group("code").strip()
        return {"kind": "code", "value": code, "language": language}

    return {"kind": "answer", "value": text}


def _token_estimate(payload: Any) -> int:
    """Approximate token usage using a conservative char-to-token ratio."""
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    # RAM_NOTE: Approximation avoids loading tokenizers and extra runtime memory.
    return max(1, len(text) // 4)


def retry(max_attempts: int = 2, fallback_prompt: FallbackPromptBuilder | None = None) -> Callable:
    """Return a retry decorator that can simplify prompts after failures."""
    attempts = max(1, int(max_attempts))

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapped(prompt: str, *args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None
            current_prompt = prompt
            for attempt in range(1, attempts + 1):
                try:
                    return func(current_prompt, *args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    if attempt >= attempts:
                        break
                    if fallback_prompt is not None:
                        current_prompt = fallback_prompt(prompt, attempt, exc)
            if last_error is None:
                raise RuntimeError("Retry wrapper exhausted without a captured error.")
            raise RuntimeError(f"Retry exhausted after {attempts} attempts: {last_error}") from last_error

        return wrapped

    return decorator


class MemoryManager:
    """Disk-backed memory for completed steps, summaries, and errors."""

    def __init__(self, storage_dir: str | Path):
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.completed_steps_path = self.storage_dir / "completed_steps.json"
        self.session_summary_path = self.storage_dir / "session_summary.txt"
        self.error_log_path = self.storage_dir / "error_log.txt"
        self.debug_log_path = self.storage_dir / "agent_debug.log"
        self.logger = _configure_logger(self.debug_log_path)

    def _read_completed_steps(self) -> list[dict[str, Any]]:
        """Load completed steps from disk."""
        if not self.completed_steps_path.exists():
            return []
        with self.completed_steps_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, list) else []

    def _write_completed_steps(self, steps: list[dict[str, Any]]) -> None:
        """Persist completed steps to disk as JSON."""
        with self.completed_steps_path.open("w", encoding="utf-8") as handle:
            json.dump(steps, handle, ensure_ascii=False, indent=2)

    def _update_session_summary(self, all_steps: list[dict[str, Any]]) -> None:
        """Update one-line session summary every third completed step."""
        if not all_steps or len(all_steps) % 3 != 0:
            return
        latest = all_steps[-3:]
        compact = "; ".join(
            f"{item.get('step_id')}={str(item.get('result', ''))[:80].strip()}" for item in latest
        )
        line = f"{_utc_iso_timestamp()} Steps {all_steps[-3]['step_id']}-{all_steps[-1]['step_id']}: {compact}\n"
        with self.session_summary_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def save_step(self, step_id: int, description: str, result: str, status: str = "success") -> None:
        """Append a completed step result to disk."""
        steps = self._read_completed_steps()
        entry = {
            "step_id": step_id,
            "description": str(description),
            "result": str(result),
            "status": str(status),
            "timestamp": _utc_iso_timestamp(),
        }
        steps.append(entry)
        self._write_completed_steps(steps)
        self._update_session_summary(steps)
        self.logger.info("step_saved step_id=%s status=%s", step_id, status)

    def save_error(self, step_id: int, description: str, error: str) -> None:
        """Record a failed step and append an error-log line."""
        line = f"{_utc_iso_timestamp()} step={step_id} desc={description} error={error}\n"
        with self.error_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self.logger.error("step_failed step_id=%s error=%s", step_id, error)

    def load_recent(self, limit: int = 3) -> list[dict[str, Any]]:
        """Return the most recent completed steps."""
        bounded = max(1, int(limit))
        steps = self._read_completed_steps()
        return steps[-bounded:]

    def prune_context(self, history: list[dict[str, Any]], max_context_tokens: int = 2048) -> list[dict[str, Any]]:
        """Compress history to one summary entry plus the latest three turns."""
        if not history:
            return []
        if _token_estimate(history) <= max_context_tokens:
            return history[-3:]
        recent = history[-3:]
        older = history[:-3]
        if not older:
            return recent
        summary_items = [str(item.get("description") or item.get("step") or "") for item in older]
        summary_text = "; ".join(value for value in summary_items if value).strip()
        compressed = {
            "step": "history_summary",
            "result": f"Steps 1-{len(older)} completed: {summary_text[:320]}",
        }
        # RAM_NOTE: Keep one compact summary + last three turns to cap active prompt footprint.
        return [compressed, *recent]


def create_llama_cpp_inference(
    model_path: str,
    *,
    n_ctx: int = 4096,
    n_gpu_layers: int = 0,
    verbose: bool = False,
    n_threads: int | None = None,
) -> InferenceFunction:
    """Create a streaming llama-cpp-python inference callable."""
    from llama_cpp import Llama

    llama = Llama(
        model_path=str(model_path),
        n_ctx=int(n_ctx),
        n_gpu_layers=int(n_gpu_layers),
        verbose=bool(verbose),
        n_threads=n_threads,
    )

    def infer(prompt: str, *, max_tokens: int = 256, temperature: float = 0.1, stop: list[str] | None = None) -> str:
        """Run one inference call against llama.cpp and return full text."""
        stream = llama(
            prompt,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            stop=stop,
            stream=True,
        )
        chunks: list[str] = []
        for token in stream:
            piece = token.get("choices", [{}])[0].get("text", "")
            if piece:
                chunks.append(piece)
        text = "".join(chunks).strip()
        # RAM_NOTE: Explicit cleanup after each inference keeps peak memory lower on 8GB systems.
        del stream
        gc.collect()
        return text

    return infer


def create_ollama_inference(
    model_name: str,
    *,
    ollama_url: str = "http://localhost:11434",
    n_ctx: int = 4096,
    timeout_seconds: int = 60,
) -> InferenceFunction:
    """Create a streaming Ollama /api/generate inference callable."""
    endpoint = f"{ollama_url.rstrip('/')}/api/generate"

    def infer(prompt: str, *, max_tokens: int = 256, temperature: float = 0.1, stop: list[str] | None = None) -> str:
        """Run one inference call against Ollama and return full text."""
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": True,
            "options": {
                "num_ctx": int(n_ctx),
                "num_gpu": 0,
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        if stop:
            payload["options"]["stop"] = stop
        response = requests.post(endpoint, json=payload, timeout=timeout_seconds, stream=True)
        response.raise_for_status()
        chunks: list[str] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            packet = json.loads(raw_line)
            piece = str(packet.get("response", ""))
            if piece:
                chunks.append(piece)
            if packet.get("done"):
                break
        text = "".join(chunks).strip()
        del response
        gc.collect()
        return text

    return infer


def create_inference_backend(
    backend: str,
    *,
    model_name: str = "qwen2.5-coder:1.5b",
    model_path: str | None = None,
    ollama_url: str = "http://localhost:11434",
    n_ctx: int = 4096,
    n_gpu_layers: int = 0,
) -> InferenceFunction:
    """Create an inference callable for either ollama or llama-cpp backends."""
    selected = str(backend or "").strip().lower()
    if selected == "ollama":
        return create_ollama_inference(model_name=model_name, ollama_url=ollama_url, n_ctx=n_ctx)
    if selected == "llama-cpp":
        if not model_path:
            raise ValueError("model_path is required when backend='llama-cpp'.")
        return create_llama_cpp_inference(
            model_path=model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )
    raise ValueError("backend must be either 'ollama' or 'llama-cpp'.")


def _planner_fallback_prompt(original_prompt: str, attempt: int, error: Exception) -> str:
    """Build a simplified planner prompt for retry attempts."""
    return (
        f"{original_prompt}\n"
        "Retry mode: return only a strict JSON array of short imperative steps. "
        f"Previous attempt {attempt} failed with: {error}."
    )


def _executor_fallback_prompt(original_prompt: str, attempt: int, error: Exception) -> str:
    """Build a simplified executor prompt for retry attempts."""
    return (
        f"{original_prompt}\n"
        "Retry mode: return either a plain answer under 3 sentences or AMBIGUOUS: <question>. "
        f"Previous attempt {attempt} failed with: {error}."
    )


def decompose_goal(goal: str, inference: InferenceFunction, max_steps: int = 12) -> list[str]:
    """Break a high-level goal into atomic steps with strict JSON output parsing."""
    prompt = PLANNER_PROMPT_TEMPLATE.format(user_goal=goal)

    @retry(max_attempts=2, fallback_prompt=_planner_fallback_prompt)
    def _planner_call(model_prompt: str) -> str:
        return inference(model_prompt, max_tokens=256, temperature=0.1, stop=["```"])

    raw = _planner_call(prompt)
    steps = _parse_step_list(raw, max_steps=max_steps)
    if steps:
        return steps
    return [f"Ask user to clarify: I could not derive executable steps for '{goal}'."]


def _execute_code_block(code: str, language: str, timeout_seconds: int = 15) -> tuple[bool, str]:
    """Execute Python or bash code deterministically and return success + output."""
    normalized_language = (language or "").strip().lower()
    if normalized_language in {"python", "py"}:
        command = [sys.executable, "-c", code]
    elif normalized_language in {"bash", "sh", "shell"}:
        if os.name == "nt":
            command = ["powershell", "-NoProfile", "-Command", code]
        else:
            command = ["bash", "-lc", code]
    else:
        return False, f"Unsupported code language: {normalized_language or 'unspecified'}"

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode == 0:
        return True, output or "Code executed successfully with no output."
    return False, output or f"Command failed with exit code {completed.returncode}."


def execute_step(
    *,
    goal_summary: str,
    current_step: str,
    previous_result: str,
    recent_context: list[dict[str, Any]],
    inference: InferenceFunction,
    execute_code: bool,
) -> dict[str, str]:
    """Execute one atomic step and return normalized status/result fields."""
    context_text = json.dumps(recent_context, ensure_ascii=False)
    prompt = EXECUTOR_PROMPT_TEMPLATE.format(
        goal_summary=goal_summary,
        previous_step_result=previous_result or "none",
        recent_context=context_text,
        current_step=current_step,
    )

    @retry(max_attempts=2, fallback_prompt=_executor_fallback_prompt)
    def _executor_call(model_prompt: str) -> str:
        return inference(model_prompt, max_tokens=320, temperature=0.1)

    raw = _executor_call(prompt)
    parsed = _parse_executor_response(raw)
    kind = parsed.get("kind", "answer")
    value = parsed.get("value", "")

    if kind == "ambiguous":
        return {"status": "skipped", "result": f"AMBIGUOUS: {value}"}

    if kind == "code":
        if not execute_code:
            return {"status": "success", "result": value}
        success, output = _execute_code_block(value, parsed.get("language", ""))
        return {"status": "success" if success else "failed", "result": output}

    return {"status": "success", "result": value}


def synthesize_results(goal: str, completed_steps: list[dict[str, Any]], inference: InferenceFunction) -> str:
    """Generate the final 3-sentence summary from completed step results."""
    compact_results = [
        {
            "step_id": item.get("step_id"),
            "description": item.get("description"),
            "status": item.get("status"),
            "result": str(item.get("result", ""))[:240],
        }
        for item in completed_steps
    ]
    prompt = SYNTHESIZER_PROMPT_TEMPLATE.format(list_of_step_results=json.dumps(compact_results, ensure_ascii=False))
    raw = inference(prompt, max_tokens=180, temperature=0.1)
    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", str(raw or "").strip()) if segment.strip()]
    if not sentences:
        return f"Goal: {goal}. No synthesized summary was generated. Review completed_steps.json for details."
    return " ".join(sentences[:3]).strip()


def run_decomposition_engine(
    goal: str,
    *,
    inference: InferenceFunction,
    memory_manager: MemoryManager,
    execute_code: bool = False,
    max_steps: int = 12,
    max_context_tokens: int = 2048,
) -> dict[str, Any]:
    """Run planner -> executor loop -> synthesizer with context pruning and disk memory."""
    trimmed_goal = str(goal or "").strip()
    if not trimmed_goal:
        raise ValueError("Goal cannot be empty.")

    memory_manager.logger.info("run_started goal=%s", trimmed_goal)
    planned_steps = decompose_goal(trimmed_goal, inference=inference, max_steps=max_steps)
    active_turns: list[dict[str, Any]] = []
    previous_result = "none"

    for step_id, step in enumerate(planned_steps, start=1):
        if step.lower().startswith("ask user to clarify:"):
            memory_manager.save_step(step_id=step_id, description=step, result=step, status="skipped")
            continue

        recent_context = memory_manager.prune_context(active_turns, max_context_tokens=max_context_tokens)
        try:
            result_payload = execute_step(
                goal_summary=trimmed_goal,
                current_step=step,
                previous_result=previous_result,
                recent_context=recent_context,
                inference=inference,
                execute_code=execute_code,
            )
            status = result_payload.get("status", "failed")
            result_text = result_payload.get("result", "")
        except Exception as exc:
            status = "failed"
            result_text = f"Step execution failed: {exc}"

        if status == "failed":
            memory_manager.save_error(step_id=step_id, description=step, error=result_text)

        memory_manager.save_step(step_id=step_id, description=step, result=result_text, status=status)
        previous_result = result_text
        active_turns.append({"step": step, "result": result_text})
        active_turns = active_turns[-3:]

    completed_steps = memory_manager._read_completed_steps()
    summary = synthesize_results(trimmed_goal, completed_steps=completed_steps, inference=inference)
    memory_manager.logger.info("run_completed steps=%s", len(completed_steps))
    return {
        "goal": trimmed_goal,
        "planned_steps": planned_steps,
        "completed_steps": completed_steps,
        "summary": summary,
        "storage_dir": str(memory_manager.storage_dir),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for standalone execution."""
    parser = argparse.ArgumentParser(description="Memory-efficient task decomposition engine.")
    parser.add_argument("goal", help="High-level goal to decompose and execute.")
    parser.add_argument(
        "--backend",
        choices=["ollama", "llama-cpp"],
        default="ollama",
        help="Inference backend (default: ollama).",
    )
    parser.add_argument("--model-name", default="qwen2.5-coder:1.5b", help="Model name for Ollama backend.")
    parser.add_argument("--model-path", default="", help="GGUF model path for llama-cpp backend.")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL.")
    parser.add_argument("--storage-dir", default="workspace\\decomposition_memory", help="Disk memory directory.")
    parser.add_argument("--max-steps", type=int, default=12, help="Maximum planner steps to execute.")
    parser.add_argument("--execute-code", action="store_true", help="Execute generated Python/bash code blocks.")
    return parser


def main() -> int:
    """Run the standalone decomposition engine CLI."""
    parser = _build_arg_parser()
    args = parser.parse_args()
    inference = create_inference_backend(
        backend=args.backend,
        model_name=args.model_name,
        model_path=args.model_path or None,
        ollama_url=args.ollama_url,
        n_ctx=4096,
        n_gpu_layers=0,
    )
    manager = MemoryManager(args.storage_dir)
    result = run_decomposition_engine(
        args.goal,
        inference=inference,
        memory_manager=manager,
        execute_code=args.execute_code,
        max_steps=max(1, args.max_steps),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
