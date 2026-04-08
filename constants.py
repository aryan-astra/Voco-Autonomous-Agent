"""Global constants shared across all VOCO modules."""

from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
WORKSPACE_PATH = BASE_DIR / "workspace"
WORKSPACE_DIR = str(WORKSPACE_PATH)
LOGS_DIR = BASE_DIR / "logs"
MEMORY_DIR = BASE_DIR / "memory"
MEMORY_VAULT_DIR = str(MEMORY_DIR / "vault")
MEMORY_FILE = MEMORY_DIR / "project_state.md"
SYSTEM_PROMPT_FILE = BASE_DIR / "prompts" / "system.md"
USER_PROFILE_FILE = str(MEMORY_DIR / "vault" / "USER.yaml")
HISTORY_FILE = str(MEMORY_DIR / "vault" / "HISTORY.jsonl")
APPS_FILE = str(MEMORY_DIR / "vault" / "APPS.md")
CONTEXT_FILE = str(MEMORY_DIR / "vault" / "CONTEXT.md")
FORMAT_FAILURE_LOG = str(MEMORY_DIR / "vault" / "failures.jsonl")

# ── Agent loop ────────────────────────────────────────────────────────────────
MAX_STEPS = 8
MAX_RETRIES = 1

# ── LLM ───────────────────────────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:4b"
OLLAMA_FAST_MODEL_CANDIDATES = [
    "qwen2.5:0.5b",
    "qwen2.5:1.5b",
    "qwen3:1.7b",
    "qwen3:4b",
]
OLLAMA_HEAVY_MODEL_CANDIDATES = [
    "qwen3:8b",
    "qwen3:4b",
    "qwen3:1.7b",
    "qwen2.5:1.5b",
    "qwen2.5:0.5b",
]
OLLAMA_NUM_CTX_SIMPLE = 2048
OLLAMA_NUM_CTX_COMPLEX = 4096
OLLAMA_NUM_CTX_MIN = 1024
OLLAMA_NUM_CTX_CONVERSATION = 1024
OLLAMA_REQUEST_TIMEOUT_SECONDS = 60
OLLAMA_CONVERSATION_TIMEOUT_SECONDS = 180

# Backward-compatible aliases
MODEL_NAME = OLLAMA_MODEL
OLLAMA_BASE_URL = OLLAMA_URL

# ── Prompt/output settings ────────────────────────────────────────────────────
ACTION_PLAN_MARKER_START = "```json"
ACTION_PLAN_MARKER_END = "```"
SYSTEM_PROMPT_BUDGET = 2000
USER_PROFILE_BUDGET = 1000
HISTORY_BUDGET = 3000
TASK_BUDGET = 4000
ROUTER_CONFIDENCE_THRESHOLD = 0.75
DEMO_INCLUDE_PROFILE_CONTEXT = False
DEMO_INCLUDE_HISTORY_CONTEXT = False

# ── Sandbox ───────────────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv", ".yaml", ".yml"}
