# VOCO - Local Windows Automation Agent

<p align="center">
  <img src="assets/voco-logo.png" alt="VOCO logo" width="260" />
</p>

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows%2011-0078D6?logo=windows)
![Runtime](https://img.shields.io/badge/LLM-Ollama-black?logo=ollama)
![Status](https://img.shields.io/badge/Status-Active%20Development-orange)
![Mode](https://img.shields.io/badge/Mode-Execution%20Only-green)

VOCO is a **local-first AI assistant** for Windows.  
It runs in terminal UI and tries to do tasks step by step (open apps, mute audio, screenshots, file tasks, browser tasks).

---

## UI Preview

![VOCO UI Screenshot](assets/ui-screenshot.png)

---

## What VOCO is (simple)

VOCO has 3 main parts:

1. **UI (`voco_ui.py`)**  
   You type commands in a clean terminal dashboard.
2. **Orchestrator (`orchestrator.py`)**  
   This is the brain loop. It decides what to run and executes tools.
3. **Tools (`tools.py`)**  
   Real actions like mute/unmute, screenshot, browser navigation, and file operations.

---

## What we completed

We implemented the full Phase-1 build and major fixes:

- Gemma/Ollama chat integration
- JSON action-plan parser flow
- Windows tool registry (browser + OS + file + profile tools)
- TUI wired to real orchestrator in background thread
- Evaluation pipeline (`eval.py`) with 20 prompts
- Memory vault files in `memory/vault/`
- Better UI logging (fixed garbled `[voco]` style output)
- Better LLM error messages for HTTP 500 / timeout
- **Local fast-path** for basic commands (`mute`, `un-mute`, screenshot, running apps)
- **Single-model pinning** to `gemma4:e2b` with adaptive context fallback
- **Closed-loop browser tools** (`browser_navigate`, `browser_get_state`, `browser_type`, `browser_click`, `browser_press_key`)
- **Stress browser suite** (`browser_stress_50_sites`) with deterministic per-site retries/timeouts + telemetry (opens each site in a separate tab by default)
- **YouTube comment export pipeline** (`youtube_comment_pipeline`) with play/pause/comment extraction + Desktop `.txt` save
- **Web codegen autofix loop** (`web_codegen_autofix`) for ChatGPT/configured assistant/free-AI generate -> run -> traceback-guided fix -> rerun
- **Desktop UI automation tools** (`get_window_state`, `click_in_window`)
- **Existing document opener** (`open_existing_document`) for opening already-present PDF/PPT/PPTX files from your PC
- **Index-backed local search** (`index_files`, `index_apps`, `search_file`) with `/index` and `/index-app`
- **Approval-gated system tools** (`run_command`, `disable_usb_device`, `add_firewall_rule`)
- **Wake-word voice pipeline module** at `voice/wake_voice.py` (optional dependencies)

---

## Important current behavior

### 1) Basic commands now work fast without LLM
For simple commands, VOCO bypasses model planning and executes directly.

Examples:
- `mute the system audio`
- `un-mute the system audio`
- `ur-mute the system audio` (typo handled)
- `open notepad and write "hello world" in notepad`
- `go to chrome and open x.com`
- `open browser and go to duckduckgo.com and search for github copilot`
- `open browser and go to example.com and click Learn more`
- `open chrome and go to chatgpt.com and write hi`
- `open youtube and search mkbhd and play the 1st video`
- `run stress-browser-50-sites` (or `run stress browser for 50 sites`)
- `run stress-youtube-comment-pipeline for "mkbhd latest video" and save as mkbhd_comments.txt`
- `create benchmark_codegen.py and run web codegen autofix`
- `open file explorer and find for AIOT-content folder`
- `open notepad and click file`
- `run command Get-Date` (requires explicit approval phrase)

### 2) Tool-first decomposition is default for non-trivial requests
VOCO now splits non-trivial prompts into atomic steps before execution.

- each step is routed with a lightweight ML router + deterministic argument extraction
- direct tool execution is used for high-confidence valid-arg routes
- fallback order is: direct router tool -> local fast-path rule -> Gemma step planner
- browser/media terms are blocked from local file-search routing to prevent misclassification

### 3) Complex commands still depend on local model health
If your machine has low free RAM/VRAM, complex model tasks can still fail or timeout.

### 4) Browser profile behavior for Playwright actions
VOCO supports explicit profile modes: **default**, **snapshot**, and **automation**.

- Use commands like `switch chrome profile to default`, `switch chrome profile to snapshot`, or
  `switch chrome profile to automation`.
- `default` tries Chrome/Edge default profile first; if locked/unavailable, fallback is snapshot then automation.
- `snapshot` uses a copied profile at `%LOCALAPPDATA%\VOCO\playwright-profiles\...`; fallback is automation.
- `automation` launches a clean browser automation profile.
- Firefox always runs in automation mode even if default/snapshot is requested.
- Responses include: `browser`, `profile_mode` (requested), `effective_profile_mode` (actual), and `launch_mode`.

### 5) Gemma 4 E2B capabilities in this setup
From `ollama show gemma4:e2b`, the local model exposes:

- completion
- tools
- thinking
- vision
- audio

VOCO currently uses text completion + tool planning/runtime paths. Vision/audio capabilities can be added as a future closed-loop extension.

### 6) Secure memory + redaction behavior
Sensitive vault data is encrypted at rest with Windows DPAPI.

- Encrypted files: `memory/vault/USER.yaml`, `memory/vault/CONTEXT.md`, `memory/project_state.md`.
- Legacy plaintext vault files are auto-migrated to encrypted envelopes on first secure load/save.
- Decryption is bound to the same Windows user context; cross-user/machine reads fail safely.
- Tool-argument redaction is enforced in orchestrator logs/history for sensitive keys
  (`password`, `secret`, `token`, `api_key`, `credential`, `auth`, `cookie`, `session`, etc.).
- `update_user_profile` arguments are logged with `value` redacted; sensitive profile keys are displayed as `[REDACTED]`.

### 7) Access-level policy (tool execution scope)
- **L1 (UI Automation):** window/control interactions.
- **L2 (Native user/admin APIs):** browser/files/process/app operations.
- **L3 (Privileged guarded APIs):** approval-gated system operations.
- **L4 (Kernel/driver):** intentionally excluded in current VOCO scope.

---

## Known issue (current)

On low-memory sessions, Ollama can return:

- `HTTP 500: model requires more system memory ...`
- or request timeout

This is not a UI crash.  
This is a model runtime resource issue.

---

## Quick start

## 1) Install dependencies

```powershell
pip install playwright pyautogui pygetwindow pywin32 pynput keyboard pyyaml scikit-learn joblib textual rich pillow pytesseract pywinauto requests
python -m playwright install chromium
```

## 2) Ensure Ollama is running

```powershell
ollama serve
```

Pull the exact model used by current VOCO config:

```powershell
ollama pull gemma4:e2b
```

## 3) Run VOCO UI

```powershell
python voco_ui.py
```

Or run with admin elevation helper:

```powershell
run_voco_admin.bat
```

Optional voice dependencies (for Ctrl+G wake-word mode):

```powershell
pip install pvporcupine sounddevice numpy faster-whisper
set PORCUPINE_ACCESS_KEY=your_key_here
```

## 4) Run benchmarks and regression gate

List available benchmark scenarios:

```powershell
python eval.py benchmark --list-scenarios
```

Run the full benchmark suite (browser + desktop + local index/search):

```powershell
python eval.py benchmark
```

Run with explicit release thresholds:

```powershell
python eval.py benchmark --min-success-rate 85 --max-avg-latency 35 --max-p95-latency 60
```

Useful options:

- `--category browser|desktop|local_index|stress` (repeatable) to scope the run
- `--scenario <scenario-id>` (repeatable) to run specific scenarios
- `--output <path>` to save JSON report at a custom location
- `--no-gate` to collect metrics without failing the process

How to interpret gate result:

- Per scenario: success/failure + total latency seconds
- Aggregate: success rate + avg/median/p95/min/max latency
- Exit code:
  - `0` = gate passed
  - `1` = gate failed (threshold miss)
- `2` = invalid benchmark args/config

Export curated learning traces (for optional future fine-tuning):

```powershell
python eval.py export-learning-data --output memory/vault/sft_command_traces.jsonl
```

Legacy 20-prompt reliability suite is still available:

```powershell
python eval.py
```

---

## Project structure

```text
Sem4-AIOT/
├── readme.md
├── voco_ui.py
├── orchestrator.py
├── llm.py
├── tools.py
├── _prompt.py
├── context.py
├── memory.py
├── constants.py
├── run_voco_admin.bat
├── eval.py
├── voice/
│   └── wake_voice.py
├── memory/
│   └── vault/
│       ├── USER.yaml
│       ├── HISTORY.jsonl
│       ├── CONTEXT.md
│       └── failures.jsonl
└── assets/
    ├── ui-screenshot.png
    └── voco-logo.png
```

---

## Evaluation snapshot

From the first full 20-prompt run:

- Success rate: **5.0%**
- Format failure rate: **95.0%**
- Main blocker: model timeout / memory pressure

This is why local fast-path and model fallback were added.

---

## Operator runbook: stress + secure memory

### 1) Stress run commands + expected outputs/gates

Run stress flows from VOCO task input:

- `run stress browser for 50 sites`
- `run stress browser for 50 sites retries 2 timeout 20`
- `run stress browser for 50 sites in same tab` (override; default is separate tabs)
- `run stress-youtube-comment-pipeline for "mkbhd latest video" and save as mkbhd_comments.txt`
- `create benchmark_codegen.py and run web codegen autofix`

Expected tool outcomes:

- `browser_stress_50_sites` returns `status=success|failure` with `requested_sites`, `successful_sites`,
  `failed_sites`, and `failures` diagnostics (per-site attempts/latency/http status).
- `youtube_comment_pipeline` returns `output_path` (Desktop `.txt`) and `comments_extracted`.
- `web_codegen_autofix` returns file `path`, run `attempts`, provider info; unresolved runs fail explicitly
  with `exhausted N attempt(s)`.

Run stress gate from CLI:

```powershell
python eval.py benchmark --category stress
python eval.py benchmark --scenario stress-browser-50-sites --scenario stress-youtube-comment-pipeline --scenario stress-web-codegen-autofix
python eval.py benchmark --category stress --min-success-rate 85 --max-avg-latency 35 --max-p95-latency 60
```

Gate behavior:

- `Gate checks: OK` and exit code `0` when thresholds pass.
- `FAIL ...` gate lines + `REGRESSION GATE FAILED.` and exit code `1` when thresholds miss.
- Exit code `2` on invalid benchmark arguments/config.

### 2) Browser profile switch modes (default/snapshot/automation)

Supported commands:

- `switch chrome profile to default`
- `switch chrome profile to snapshot`
- `switch chrome profile to automation`
- `switch edge profile mode to default`

Mode behavior:

- **default**: attempt Chrome/Edge default profile; fallback to snapshot/automation if unavailable/locked.
- **snapshot**: launch from copied profile snapshot; fallback to automation if snapshot prep/launch fails.
- **automation**: launch isolated automation profile directly.
- **firefox**: always automation (default/snapshot requests downgrade with launch note).

### 3) YouTube comment pipeline

Usage:

- `run stress-youtube-comment-pipeline for "mkbhd latest video" and save as mkbhd_comments.txt`
- Optional hints in the command are supported: `20 comments`, `pause after 3 seconds`, `open in notepad`,
  `dry run`, and browser choice (`chrome|edge|firefox`).

Output:

- Saved to `%USERPROFILE%\Desktop\<filename>.txt`
- Default filename when omitted: `youtube_comments_<timestamp>.txt`

### 4) Web codegen autofix workflow

Usage:

- `create my_script.py and run web codegen autofix`
- Optional hints: `fix 3 times`, `run timeout 60`, `dry run`

Workflow:

1. Generate Python candidate (`web_assistant_command` profile/env if configured; otherwise free AI Ollama path).
2. Write script into `workspace\`.
3. Run script with bounded timeout.
4. On runtime failure, retry with traceback-guided full-script regeneration up to max rounds.

Caveats:

- Requires configured web assistant command or healthy local Ollama free-AI fallback.
- Filename is sanitized to workspace-safe `.py`.
- Can still fail after bounded attempts; failure payload includes last attempt diagnostics.

### 5) Desktop app/file/media availability + open flows

Usage examples:

- `check if spotify is available`
- `check if powerpoint is installed`
- `check if pdf handler is available`
- `open "C:\path\to\slides.pptx"`
- `open pdf`
- `open spotify and play lo-fi beats`

Behavior:

- Availability checks return structured `available` + fallback hints.
- `.pdf/.ppt/.pptx` opens are preflighted through default-handler checks.
- `open pdf` / `open pptx` launches the default handler app directly.
- Spotify flow prechecks install availability, opens Spotify, then best-effort search/play automation.

### 6) Secure memory + sensitive-data handling

- At-rest encryption uses Windows DPAPI envelopes (`schema=voco.secure-memory.v1`,
  `protection=windows-dpapi-current-user`) for:
  - `memory/vault/USER.yaml`
  - `memory/vault/CONTEXT.md`
  - `memory/project_state.md`
- Legacy plaintext payloads auto-migrate to encrypted envelopes on first secure read/write.
- Tool args with sensitive key names are logged as `[REDACTED]` in history/action traces.
- `update_user_profile` value arguments are redacted in orchestrator logs/history.

Current limitation:

- VOCO does **not** run full free-text PII detection on arbitrary task text/output before history logging.

### 7) Troubleshooting (common failures)

- **Profile lock / browser profile unavailable**
  - Symptom: launch note mentions profile lock/unavailable or switch fails.
  - Action: close existing Chrome/Edge windows and retry, or switch to `snapshot` / `automation`.

- **Missing app / missing default handler**
  - Symptom: app is “not launchable” or no handler for `.pdf/.pptx`.
  - Action: run availability check first, install/repair app or set default handler, then retry.

- **Selector drift / element not found**
  - Symptom: selector/element-not-found errors in browser or desktop steps.
  - Action: refresh state (`browser_get_state` / `get_window_state` path) and retry with updated element label.

- **Retries exhausted**
  - Symptom: `Retries exhausted for step ...` or `web-codegen autofix exhausted N attempt(s)`.
  - Action: rerun with simpler target, increase timeout/fix rounds where supported, or use dry-run to validate flow.

---

## Final note

VOCO is now much more usable for daily basic commands on this device.  
For complex tasks, stability still depends on available memory/CPU headroom for `gemma4:e2b`.

