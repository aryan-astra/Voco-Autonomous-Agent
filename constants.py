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
MAX_RETRIES = 2

# ── LLM ───────────────────────────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:4b"
OLLAMA_FAST_MODEL_CANDIDATES = [
    "qwen3:4b",
]
OLLAMA_HEAVY_MODEL_CANDIDATES = [
    "qwen3:4b",
]
OLLAMA_NUM_CTX_SIMPLE = 2048
OLLAMA_NUM_CTX_COMPLEX = 2048
OLLAMA_NUM_CTX_MIN = 512
OLLAMA_NUM_CTX_CONVERSATION = 2048
OLLAMA_CTX_FALLBACK_LEVELS = [8192, 6144, 4096, 3072, 2048, 1536, 1024, 768, 512]
OLLAMA_REQUEST_TIMEOUT_SECONDS = 120
OLLAMA_CONVERSATION_TIMEOUT_SECONDS = 90
OLLAMA_CPU_ONLY = True

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
CONTEXT_PRUNE_THRESHOLD = 0.85
AUTONOMY_MODE = True
HUMAN_APPROVAL_DISABLED = True

# ── Voice ──────────────────────────────────────────────────────────────────────
PTT_KEY = "space"
PTT_DEBOUNCE_MS = 100
VOICE_MODEL_ID = "distil-whisper/distil-small.en"
VOICE_PREALLOCATE_BUFFER_SEC = 30

# ── Persistence/runtime ────────────────────────────────────────────────────────
DB_WAL_MODE = True
WATCHDOG_DEBOUNCE_SEC = 5

# ── Sandbox ───────────────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv", ".yaml", ".yml"}
