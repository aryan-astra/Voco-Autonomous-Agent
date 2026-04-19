"""VOCO TUI frontend prototype using Textual (no backend logic)."""

import asyncio
import queue
import threading
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Input, Label, RichLog, Rule, Static

from constants import OLLAMA_MODEL
from context import AgentContext
from orchestrator import run as orchestrator_run, start_fs_watcher, stop_fs_watcher


_agent_context = AgentContext()
_MODEL_META_LABEL = f"{OLLAMA_MODEL} • Local LLM"


class VocoApp(App):
    """Modern minimalist VOCO dashboard mockup."""

    TITLE = "VOCO"
    SUB_TITLE = "Autonomous Agent Prototype"

    BINDINGS = [
        Binding("ctrl+g", "voice_toggle", "Voice", show=True, priority=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    # Mascot — 2 frames: eyes open / blink.
    MASCOT_FRAMES = [
        # Frame 0 — eyes open
        (
            "    ▄▄▄▄▄▄▄\n"
            "   █ ■   ■ █\n"
            "   █  ─ ─  █\n"
            "   █▄▄▄▄▄▄▄█\n"
            "   ▐█▌   ▐█▌"
        ),
        # Frame 1 — blink (eyes closed)
        (
            "    ▄▄▄▄▄▄▄\n"
            "   █ ─   ─ █\n"
            "   █  ─ ─  █\n"
            "   █▄▄▄▄▄▄▄█\n"
            "   ▐█▌   ▐█▌"
        ),
    ]

    # Textual CSS controls almost the entire visual identity of this prototype.
    # Palette is intentionally muted and modern to avoid retro terminal styling.
    CSS = """
    Screen {
        background: #000000;
        color: #D4D4D4;
        align: center middle;
    }

    #root {
        width: 100%;
        height: 100%;
        align-horizontal: center;
        padding: 1 2;
    }

    #upper-zone {
        width: 86;
        max-width: 90;
        min-height: 20;
        height: 1fr;
    }

    #dashboard {
        width: 100%;
        border: dashed #C96B45;
        border-title-color: #C96B45;
        border-title-style: bold;
        background: #000000;
        padding: 1 2;
    }

    #dashboard-row {
        width: 100%;
        height: 1fr;
    }

    #left-column {
        width: 1fr;
        content-align: center middle;
        padding: 1 1;
    }

    #left-column .welcome {
        color: #D4D4D4;
        text-style: bold;
        margin-bottom: 1;
    }

    #mascot {
        color: #C96B45;
        text-style: bold;
        text-align: center;
        margin: 1 0;
    }

    #left-column .meta {
        color: #7E7E81;
    }

    #voice-status {
        color: #7E7E81;
        margin-top: 1;
        text-style: bold;
    }

    #right-column {
        width: 1fr;
        padding: 1 1;
    }

    #right-column Rule {
        color: #C96B45;
        margin: 0 0 1 0;
    }

    .section-title {
        color: #C96B45;
        text-style: bold;
        margin-bottom: 1;
    }

    .item {
        color: #D4D4D4;
        margin: 0 0 1 0;
    }

    .time {
        color: #7E7E81;
    }

    #conversation {
        width: 100%;
        height: 10;
        display: block;
        border-top: dashed #3A3A3A;
        padding: 0 1;
    }

    #chat-log {
        width: 100%;
        height: 100%;
        border: none;
        background: transparent;
        color: #D4D4D4;
        padding: 0;
        scrollbar-color: #C96B45;
        scrollbar-background: #000000;
    }

    #divider {
        width: 86;
        max-width: 90;
        color: #7E7E81;
    }

    #prompt-row {
        width: 86;
        max-width: 90;
        height: 3;
        align: left middle;
        background: transparent;
    }

    #hotkeys-inline {
        width: auto;
        color: #C96B45;
        text-style: bold;
        padding: 0 0 0 2;
    }

    #prompt-prefix {
        width: auto;
        color: #D4D4D4;
        padding: 0 1 0 0;
        text-style: bold;
    }

    #command-input {
        width: 1fr;
        border: none;
        background: transparent;
        color: #D4D4D4;
        padding: 0;
    }

    #command-input:focus {
        border: none;
        background: transparent;
    }

    Footer {
        background: #000000;
        color: #9CA3AF;
        dock: bottom;
    }

    Footer .footer--key {
        color: #C96B45;
    }

    Footer .footer--description {
        color: #D4D4D4;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._mascot_index = 0
        self._task_queue: queue.Queue[str | None] = queue.Queue()
        self._worker_stop = threading.Event()
        self._worker_thread = threading.Thread(target=self._task_worker_loop, daemon=True)
        self._voice_agent = None
        self._voice_enabled = False
        self._voice_degraded = False
        self._fs_watcher_observer = None

    def compose(self) -> ComposeResult:
        # The UI is split into three vertical bands:
        # 1) Upper content zone (dashboard or conversation stream)
        # 2) A subtle horizontal divider line
        # 3) A clean borderless command prompt row
        with Vertical(id="root"):
            with Container(id="upper-zone"):
                # Initial dashboard view shown before the first submitted command.
                with Container(id="dashboard"):
                    with Horizontal(id="dashboard-row"):
                        # Left identity/status column.
                        with Vertical(id="left-column"):
                            yield Label("Welcome back, Developer!", classes="welcome")
                            yield Static(self.MASCOT_FRAMES[0], id="mascot")
                            yield Label(_MODEL_META_LABEL, classes="meta")
                            yield Label("/workspace/voco/apps", classes="meta")
                            yield Label("Voice: OFF (direct mode default)", id="voice-status")

                        # Right activity/information column.
                        with Vertical(id="right-column"):
                            yield Label("Recent activity", classes="section-title")
                            yield Label("[#7E7E81]1m ago[/]  Updated system memory", classes="item")
                            yield Label("[#7E7E81]4m ago[/]  Synced local toolchain", classes="item")
                            yield Label("[#7E7E81]9m ago[/]  Indexed workspace files", classes="item")
                            yield Rule()
                            yield Label("What's new", classes="section-title")
                            yield Label("/agents  — spawn sub-agents", classes="item")
                            yield Label("/help    — list all commands", classes="item")
                            yield Label("/workspace — view workspace", classes="item")

                # Conversation log remains visible and scrollable under dashboard.
                with Container(id="conversation"):
                    yield RichLog(id="chat-log", markup=True, highlight=False, wrap=True)

            yield Rule(id="divider")

            with Horizontal(id="prompt-row"):
                yield Label(">", id="prompt-prefix")
                yield Input(placeholder='Try "edit <filepath> to ..."', id="command-input")
                yield Label("^G Voice   Q Quit", id="hotkeys-inline")

            yield Footer()

    def on_mount(self) -> None:
        # Border title is set at runtime to keep string formatting explicit.
        self.query_one("#dashboard", Container).border_title = " VOCO v1.0.0 "
        self.query_one("#command-input", Input).focus()
        self.set_interval(1.2, self._animate_mascot)
        self._fs_watcher_observer = start_fs_watcher()
        if not self._worker_thread.is_alive():
            self._worker_thread.start()
        self._probe_voice_runtime()

    def on_unmount(self) -> None:
        self._worker_stop.set()
        self._task_queue.put_nowait(None)
        try:
            stop_fs_watcher(self._fs_watcher_observer)
        except Exception:
            pass
        self._fs_watcher_observer = None
        if self._voice_agent is not None:
            try:
                self._voice_agent.stop()
            except Exception:
                pass
            self._voice_agent = None
            self._voice_enabled = False
        self._set_voice_status_indicator("OFF")

    def _animate_mascot(self) -> None:
        """Animate mascot while dashboard is visible for subtle liveliness."""
        dashboard = self.query_one("#dashboard", Container)
        if not dashboard.display:
            return

        self._mascot_index = (self._mascot_index + 1) % len(self.MASCOT_FRAMES)
        self.query_one("#mascot", Static).update(self.MASCOT_FRAMES[self._mascot_index])

    def _activate_conversation(self) -> RichLog:
        """Return the chat log widget while keeping the dashboard visible."""
        return self.query_one("#chat-log", RichLog)

    def _render_help(self, chat_log: RichLog) -> None:
        """Render available mock commands for the prototype."""
        chat_log.write(Text("[VOCO] Available Commands", style="#C96B45"))
        chat_log.write(Text("  /help             Show command list", style="#D4D4D4"))
        chat_log.write(Text("  /agents           Spawn mock sub-agent", style="#D4D4D4"))
        chat_log.write(Text("  /security-review  Start mock security pass", style="#D4D4D4"))
        chat_log.write(Text("  /resume           Continue last workflow", style="#D4D4D4"))
        chat_log.write(Text("  /index            Build full file index", style="#D4D4D4"))
        chat_log.write(Text("  /index-app        Build installed app index", style="#D4D4D4"))
        chat_log.write(Text("  Ctrl+G            Toggle voice (direct default; wake-word optional)", style="#D4D4D4"))

    async def _handle_slash_command(self, command: str, chat_log: RichLog) -> bool:
        """Handle slash commands and return True when a command was consumed."""
        normalized = command.strip().lower()

        if normalized == "/help":
            self._render_help(chat_log)
            return True

        if normalized == "/agents":
            await asyncio.sleep(0.25)
            chat_log.write(Text("[VOCO] Sub-agent orchestrator online.", style="#C96B45"))
            chat_log.write(Text("[AGENT-1] Planner ready", style="#D4D4D4"))
            chat_log.write(Text("[AGENT-2] Coder ready", style="#D4D4D4"))
            return True

        if normalized == "/security-review":
            await asyncio.sleep(0.25)
            chat_log.write(Text("[VOCO] Running security review (mock)...", style="#C96B45"))
            chat_log.write(Text("No critical findings. 2 low-risk suggestions.", style="#D4D4D4"))
            return True

        if normalized == "/resume":
            await asyncio.sleep(0.25)
            chat_log.write(Text("[VOCO] Restored previous task context.", style="#C96B45"))
            chat_log.write(Text("Ready for next instruction.", style="#D4D4D4"))
            return True

        if normalized == "/index":
            chat_log.write(Text("[VOCO] Starting full file index build...", style="#C96B45"))
            self._handle_user_input("index files on my pc")
            return True

        if normalized == "/index-app":
            chat_log.write(Text("[VOCO] Starting application index build...", style="#C96B45"))
            self._handle_user_input("index apps")
            return True

        if normalized == "/back":
            chat_log.write(Text("[VOCO] Already on home dashboard.", style="#C96B45"))
            return True

        if normalized == "/workspace":
            await asyncio.sleep(0.15)
            chat_log.write(Text("[VOCO] Workspace", style="#C96B45"))
            chat_log.write(Text("  Root   /workspace/voco/apps", style="#D4D4D4"))
            chat_log.write(Text(f"  Model  {_MODEL_META_LABEL}", style="#D4D4D4"))
            chat_log.write(Text("  Files  12 indexed  •  3 modified", style="#D4D4D4"))
            return True

        return False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Frontend-only mock flow:
        # capture command -> clear input -> swap to chat view -> simulated response.
        command = event.value.strip()
        if not command:
            return

        input_box = self.query_one("#command-input", Input)
        input_box.value = ""

        chat_log = self._activate_conversation()

        chat_log.write(Text(f"> {command}", style="#D4D4D4"))

        if await self._handle_slash_command(command, chat_log):
            return

        self._handle_user_input(command)

    def _update_output(self, message: str, style: str) -> None:
        """Write a formatted status line to the output panel."""
        chat_log = self._activate_conversation()
        chat_log.write(Text(message, style=style))

    def _set_voice_status_indicator(
        self,
        status: str,
        detail: str = "",
        color: str = "#7E7E81",
    ) -> None:
        suffix = f" ({detail})" if detail else ""
        indicator = self.query_one("#voice-status", Label)
        indicator.update(f"Voice: {status}{suffix}")
        indicator.styles.color = color

    def _probe_voice_runtime(self) -> None:
        try:
            from voice.wake_voice import VocoVoice

            dep_status = VocoVoice.startup_status()
        except Exception as exc:
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "voice unavailable", "red")
            self._update_output(f"[VOCO VOICE] Voice init probe failed: {exc}", "red")
            return

        if not dep_status["available"]:
            missing = ", ".join(str(name) for name in dep_status["missing"])
            install_hint = str(dep_status.get("install_hint", "")).strip()
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "install voice deps", "yellow")
            message = f"[VOCO VOICE] Missing deps: {missing}."
            if install_hint:
                message = f"{message} Run: {install_hint}"
            self._update_output(message, "yellow")
            return

        if not dep_status["runtime_ready"]:
            runtime_hint = str(dep_status.get("runtime_hint", "")).strip()
            fallback_note = str(dep_status.get("fallback_note", "")).strip()
            wake_model_dir = str(dep_status.get("wake_model_dir", "")).strip()
            hf_cache = str(dep_status.get("hf_cache", "")).strip()
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "check runtime cache", "red")
            runtime_error = str(dep_status.get("error", "")).strip()
            message = "[VOCO VOICE] Wake/ASR assets not ready."
            if runtime_hint:
                message = f"{message} {runtime_hint}"
            elif runtime_error:
                message = f"{message} Error: {runtime_error}"
            if fallback_note:
                message = f"{message} {fallback_note}"
            if wake_model_dir:
                message = f"{message} Wake dir: {wake_model_dir}"
            elif hf_cache:
                message = f"{message} HF cache: {hf_cache}"
            self._update_output(message, "red")
            return

        if dep_status["vad_mode"] == "webrtcvad":
            self._voice_degraded = False
            self._set_voice_status_indicator("READY", "direct mode default, WebRTC VAD")
            return

        self._voice_degraded = True
        self._set_voice_status_indicator("DEGRADED", "direct mode, silence VAD fallback", "yellow")
        self._update_output(
            "[VOCO VOICE] webrtcvad missing. Direct mode active with silence-heuristic VAD fallback.",
            "yellow",
        )

    def _handle_voice_status_event(self, message: str, level: str = "info") -> None:
        color_map = {
            "info": "white",
            "step": "cyan",
            "ready": "green",
            "degraded": "yellow",
            "error": "red",
        }
        self._update_output(f"[VOCO VOICE] {message}", color_map.get(level, "white"))

        if level == "ready":
            if self._voice_agent is not None and getattr(self._voice_agent, "vad_mode", "") == "webrtcvad":
                self._voice_degraded = False
                self._set_voice_status_indicator("ON", "direct mode, WebRTC VAD", "green")
                return
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "direct mode, silence VAD fallback", "yellow")
            return

        if level == "degraded":
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "direct mode, silence VAD fallback", "yellow")
            return

        if level == "error":
            self._voice_enabled = False
            self._voice_agent = None
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "voice runtime error", "red")

    def _queue_voice_command(self, spoken: str) -> None:
        command = spoken.strip()
        if not command:
            return

        app_thread_id = getattr(self, "_thread_id", None)
        if app_thread_id is not None and app_thread_id == threading.get_ident():
            self._handle_user_input(command)
            return
        self.call_from_thread(self._handle_user_input, command)

    def _emit_to_ui(self, message: str, level: str = "info") -> None:
        """
        Thread-safe callback used by orchestrator.run to stream progress.
        """
        color_map = {
            "info": "white",
            "step": "cyan",
            "success": "green",
            "error": "red",
        }
        color = color_map.get(level, "white")
        safe_message = escape(message).replace("\r", "").strip()
        app_thread_id = getattr(self, "_thread_id", None)
        if app_thread_id is not None and app_thread_id == threading.get_ident():
            self._update_output(safe_message, color)
            return
        self.call_from_thread(self._update_output, safe_message, color)

    def _handle_user_input(self, task: str) -> None:
        """Queue user tasks for a single persistent worker thread."""
        if not task.strip():
            return
        self._task_queue.put(task)
        pending = self._task_queue.qsize()
        self._update_output(f"[VOCO] Queued task ({pending} pending).", "white")

    def _task_worker_loop(self) -> None:
        """Run tasks sequentially in one worker thread for tool/runtime stability."""
        while not self._worker_stop.is_set():
            try:
                task = self._task_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if task is None:
                self._task_queue.task_done()
                break

            try:
                orchestrator_run(
                    task=task,
                    context=_agent_context,
                    ui_callback=lambda msg, lvl: self._emit_to_ui(msg, lvl),
                )
            except Exception as exc:
                self._emit_to_ui(f"[VOCO] FATAL ERROR: {exc}", "error")
            finally:
                self._task_queue.task_done()

    def action_voice_toggle(self) -> None:
        chat_log = self._activate_conversation()
        if self._voice_enabled and self._voice_agent is not None:
            try:
                self._voice_agent.stop()
            except Exception as exc:
                chat_log.write(Text(f"[VOCO VOICE] Stop failed: {exc}", style="red"))
                return
            self._voice_enabled = False
            self._voice_agent = None
            self._voice_degraded = False
            self._set_voice_status_indicator("OFF")
            chat_log.write(Text("[VOCO VOICE] Stopped.", style="#C96B45"))
            return

        self._set_voice_status_indicator("STARTING", "direct mode")
        chat_log.write(
            Text(
                "[VOCO VOICE] Initializing direct command mode + ASR (openai/whisper-medium)...",
                style="#C96B45",
            )
        )

        try:
            from voice.wake_voice import VocoVoice

            self._voice_agent = VocoVoice(
                on_command_callback=self._queue_voice_command,
                on_status_callback=lambda msg, lvl: self.call_from_thread(self._handle_voice_status_event, msg, lvl),
                interaction_mode=VocoVoice.INTERACTION_MODE_DIRECT,
            )
            self._voice_agent.start()
            self._voice_enabled = True
            self._voice_degraded = getattr(self._voice_agent, "vad_mode", "") != "webrtcvad"
            if self._voice_degraded:
                self._set_voice_status_indicator("DEGRADED", "direct mode, silence VAD fallback", "yellow")
                chat_log.write(
                    Text(
                        "[VOCO VOICE] Started in direct mode (silence VAD fallback). Wake-word mode is optional.",
                        style="yellow",
                    )
                )
                return
            self._set_voice_status_indicator("ON", "direct mode, WebRTC VAD", "green")
            chat_log.write(Text("[VOCO VOICE] Direct command listener started. Wake-word mode is optional.", style="#C96B45"))
        except Exception as exc:
            self._voice_agent = None
            self._voice_enabled = False
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "voice unavailable", "red")
            chat_log.write(Text(f"[VOCO VOICE] Failed to start: {exc}", style="red"))


if __name__ == "__main__":
    VocoApp().run()
