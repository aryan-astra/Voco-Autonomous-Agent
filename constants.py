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
OLLAMA_NUM_CTX_SIMPLE = 512
OLLAMA_NUM_CTX_COMPLEX = 1024
OLLAMA_NUM_CTX_MIN = 256
OLLAMA_NUM_CTX_CONVERSATION = 512
OLLAMA_CTX_FALLBACK_LEVELS = [1024, 768, 512, 384, 256]
OLLAMA_REQUEST_TIMEOUT_SECONDS = 45
OLLAMA_CONVERSATION_TIMEOUT_SECONDS = 60
OLLAMA_CPU_ONLY = False
GPU_LAYERS = 0
GPU_LAYERS_ENV = "VOCO_NUM_GPU_LAYERS"

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
ROUTER_HYBRID_MODE = True
ROUTE_CONTRACTS_ENABLED = True
ROUTE_CLASSIFIER_GUARD_ENABLED = True
ROUTE_CLASSIFIER_MIN_CONFIDENCE = 0.72
DEMO_INCLUDE_PROFILE_CONTEXT = False
DEMO_INCLUDE_HISTORY_CONTEXT = False
CONTEXT_PRUNE_THRESHOLD = 0.85
AUTONOMY_MODE = True
HUMAN_APPROVAL_DISABLED = True

# ── Voice ──────────────────────────────────────────────────────────────────────
PTT_KEY = "space"
PTT_DEBOUNCE_MS = 100
VOICE_MODEL_ID = "tiny"
VOICE_TRANSCRIBE_LANGUAGE = "en"
VOICE_COMPUTE_TYPE = "int8"
VOICE_PREALLOCATE_BUFFER_SEC = 30

# ── Persistence/runtime ────────────────────────────────────────────────────────
DB_WAL_MODE = True
WATCHDOG_DEBOUNCE_SEC = 5

# ── Indexing / search scope ────────────────────────────────────────────────────
SAMPLE_SEARCH_SPACE = BASE_DIR.parent / "sample-search-space"
INDEX_SCOPE_DEFAULT = "sample"  # sample | quick | full
INDEX_QUICK_USER_FOLDERS = ("Desktop", "Documents", "Downloads")
CONTENT_READABLE_EXTENSIONS = frozenset(
    {
        ".py",
        ".txt",
        ".md",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".html",
        ".js",
        ".ts",
        ".css",
        ".xml",
        ".toml",
        ".ini",
        ".bat",
        ".ps1",
        ".sh",
        ".log",
        ".rst",
        ".cfg",
    }
)
MAX_CONTENT_READ_BYTES = 4096
MAX_INDEXABLE_FILE_BYTES = 10 * 1024 * 1024

# ── MCP sidecar configuration ──────────────────────────────────────────────────
MCP_ENABLED = True
MCP_PLAYWRIGHT_PORT = 8931
MCP_PLAYWRIGHT_URL = f"http://localhost:{MCP_PLAYWRIGHT_PORT}"
MCP_DEVTOOLS_PORT = 8932
MCP_DEVTOOLS_URL = f"http://localhost:{MCP_DEVTOOLS_PORT}"

# ── Sandbox ───────────────────────────────────────────────────────────────────
ALLOWED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv", ".yaml", ".yml"}
