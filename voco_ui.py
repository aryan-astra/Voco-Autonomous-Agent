"""VOCO TUI frontend prototype using Textual (no backend logic)."""

import asyncio
import queue
import threading

from startup_check import run_startup_check

try:
    _STARTUP_STATUS = run_startup_check(autofix=True, strict=False)
except Exception as _startup_exc:  # pragma: no cover - defensive startup guard
    _STARTUP_STATUS = {"ok": False, "issues": [f"startup check failed: {_startup_exc}"]}

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, Input, Label, RichLog, Rule, Static, Tree

import constants
from context import AgentContext
from orchestrator import run as orchestrator_run, start_fs_watcher, stop_fs_watcher


_agent_context = AgentContext()
class VocoApp(App):
    """Modern minimalist VOCO dashboard mockup."""

    TITLE = "VOCO"
    SUB_TITLE = "Autonomous Agent Prototype"

    BINDINGS = [
        Binding("c", "clear_log", "Clear", show=True),
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

    #task-tree {
        width: 100%;
        height: 1fr;
        color: #D4D4D4;
        border: dashed #3A3A3A;
        padding: 0 1;
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
        self._ptt_recording = False
        self._ptt_transcribing = False
        self._ptt_transcribe_thread: threading.Thread | None = None
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
                            yield Label(f"Text model: {constants.OLLAMA_MODEL}", id="text-model", classes="meta")
                            yield Label("Voice model: probing...", id="voice-model", classes="meta")
                            yield Label(str(constants.WORKSPACE_PATH), classes="meta")
                            yield Label("Voice: OFF (PTT default)", id="voice-status")

                        # Right activity/information column.
                        with Vertical(id="right-column"):
                            yield Label("Task Progress", classes="section-title")
                            yield Tree("Task Progress", id="task-tree")

                # Conversation log remains visible and scrollable under dashboard.
                with Container(id="conversation"):
                    yield RichLog(id="chat-log", markup=True, highlight=False, wrap=True)

            yield Rule(id="divider")

            with Horizontal(id="prompt-row"):
                yield Label(">", id="prompt-prefix")
                yield Input(placeholder='Try "edit <filepath> to ..."', id="command-input")
                yield Label("Space PTT   C Clear   Q Quit", id="hotkeys-inline")

            yield Footer()

    def on_mount(self) -> None:
        # Border title is set at runtime to keep string formatting explicit.
        self.query_one("#dashboard", Container).border_title = " VOCO v1.0.0 "
        self.query_one("#command-input", Input).focus()
        self._set_text_model_indicator(constants.OLLAMA_MODEL)
        self.set_interval(1.2, self._animate_mascot)
        self._fs_watcher_observer = start_fs_watcher()
        if not self._worker_thread.is_alive():
            self._worker_thread.start()
        self._probe_voice_runtime()
        self.action_voice_toggle()
        print("[VOCO REFACTOR COMPLETE]")

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
            self._ptt_recording = False
        try:
            self._set_voice_status_indicator("OFF")
        except NoMatches:
            pass

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
        chat_log.write(Text("  /index-sample     Build sample file index", style="#D4D4D4"))
        chat_log.write(Text("  /index-quick      Build quick file index", style="#D4D4D4"))
        chat_log.write(Text("  /index-full       Build full file index (slow)", style="#D4D4D4"))
        chat_log.write(Text("  /index-app        Build installed app index", style="#D4D4D4"))
        chat_log.write(Text("  Space             Start/stop push-to-talk capture", style="#D4D4D4"))
        chat_log.write(Text("  C                 Clear conversation log", style="#D4D4D4"))

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

        if normalized == "/index-sample":
            chat_log.write(Text("[VOCO] Starting sample-space file index build...", style="#C96B45"))
            self._handle_user_input("index files sample")
            return True

        if normalized in {"/index", "/index-quick"}:
            chat_log.write(Text("[VOCO] Starting quick file index build...", style="#C96B45"))
            self._handle_user_input("index files quick")
            return True

        if normalized == "/index-full":
            chat_log.write(Text("[VOCO] Starting full file index build (may take a while)...", style="#C96B45"))
            self._handle_user_input("index files on my pc full")
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
            chat_log.write(Text(f"  Root   {constants.WORKSPACE_PATH}", style="#D4D4D4"))
            text_model = str(self.query_one("#text-model", Label).renderable).replace("Text model:", "").strip()
            voice_model = str(self.query_one("#voice-model", Label).renderable).replace("Voice model:", "").strip()
            chat_log.write(Text(f"  Text   {text_model}", style="#D4D4D4"))
            chat_log.write(Text(f"  Voice  {voice_model}", style="#D4D4D4"))
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

    async def on_key(self, event) -> None:
        if event.key == constants.PTT_KEY and self._voice_enabled and self._voice_agent is not None:
            event.stop()
            self.action_ptt_toggle()

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
        try:
            indicator = self.query_one("#voice-status", Label)
        except NoMatches:
            return
        indicator.update(f"Voice: {status}{suffix}")
        indicator.styles.color = color

    def _set_text_model_indicator(self, model_name: str, color: str = "#7E7E81") -> None:
        try:
            indicator = self.query_one("#text-model", Label)
        except NoMatches:
            return
        resolved = str(model_name or constants.OLLAMA_MODEL).strip() or constants.OLLAMA_MODEL
        indicator.update(f"Text model: {resolved}")
        indicator.styles.color = color

    def _set_voice_model_indicator(self, model_name: str, color: str = "#7E7E81") -> None:
        try:
            indicator = self.query_one("#voice-model", Label)
        except NoMatches:
            return
        resolved = str(model_name or "unknown").strip() or "unknown"
        indicator.update(f"Voice model: {resolved}")
        indicator.styles.color = color

    def _probe_voice_runtime(self) -> None:
        try:
            from voice.wake_voice import VocoVoice

            dep_status = VocoVoice.startup_status()
        except Exception as exc:
            self._voice_degraded = True
            self._set_voice_model_indicator("unavailable", "red")
            self._set_voice_status_indicator("DEGRADED", "voice unavailable", "red")
            self._update_output(f"[VOCO VOICE] Voice init probe failed: {exc}", "red")
            return

        default_voice_model = str(dep_status.get("whisper_model_default", "")).strip() or constants.VOICE_MODEL_ID
        self._set_voice_model_indicator(default_voice_model)

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
            hf_cache = str(dep_status.get("hf_cache", "")).strip()
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "check runtime cache", "red")
            runtime_error = str(dep_status.get("error", "")).strip()
            message = "[VOCO VOICE] ASR assets not ready."
            if runtime_hint:
                message = f"{message} {runtime_hint}"
            elif runtime_error:
                message = f"{message} Error: {runtime_error}"
            if hf_cache:
                message = f"{message} HF cache: {hf_cache}"
            self._update_output(message, "red")
            return

        if str(dep_status.get("vad_mode", "")).strip() in {"webrtcvad", "ptt-only"}:
            self._voice_degraded = False
            self._set_voice_status_indicator("READY", "PTT")
            return

        self._voice_degraded = True
        self._set_voice_status_indicator("DEGRADED", "fallback VAD", "yellow")
        vad_install_hint = str(dep_status.get("vad_install_hint", "pip install webrtcvad")).strip()
        self._update_output(
            (
                "[VOCO VOICE] webrtcvad missing. Push-to-talk mode active with silence-heuristic VAD fallback. "
                f"Run: {vad_install_hint}"
            ),
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
            vad_mode = str(getattr(self._voice_agent, "vad_mode", "")).strip().lower() if self._voice_agent else ""
            if vad_mode in {"webrtcvad", "ptt-only"}:
                self._voice_degraded = False
                suffix = "PTT recording" if self._ptt_recording else "PTT"
                color = "#C96B45" if self._ptt_recording else "green"
                self._set_voice_status_indicator("ON", suffix, color)
                return
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "PTT degraded", "yellow")
            return

        if level == "degraded":
            self._voice_degraded = True
            self._set_voice_status_indicator("DEGRADED", "PTT degraded", "yellow")
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

    def _update_task_tree(self, context: AgentContext) -> None:
        tree = self.query_one("#task-tree", Tree)
        tree.clear()
        root = tree.root
        steps = list(context.decomposed_steps or [])
        if not steps:
            for item in context.steps:
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool", "")).strip() or "step"
                reason = str(item.get("reason", "")).strip()
                label = f"{tool} - {reason}" if reason else tool
                steps.append(label)
        if not steps:
            root.add("[#7E7E81]Waiting for plan...[/]")
            tree.refresh()
            return

        for idx, step in enumerate(steps, start=1):
            status = "pending"
            color = "#F4C96A"
            for res in reversed(context.tool_results):
                if res.get("step") == idx:
                    nested = res.get("result", {}) if isinstance(res.get("result"), dict) else {}
                    status = str(nested.get("status", res.get("status", "unknown")))
                    break
            if status == "success":
                color = "#4CAF50"
            elif status in ("error", "failure"):
                color = "#E53935"
            elif status == "running":
                color = "#29B6F6"
            root.add(f"[{color}][Step {idx}][/color] {step}", expand=True)
        tree.refresh()

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
            self._update_task_tree(_agent_context)
            return
        self.call_from_thread(self._update_output, safe_message, color)
        self.call_from_thread(self._update_task_tree, _agent_context)

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
            if self._ptt_recording:
                try:
                    self._voice_agent.end_push_to_talk()
                except Exception:
                    pass
                self._ptt_recording = False
            try:
                self._voice_agent.stop()
            except Exception as exc:
                chat_log.write(Text(f"[VOCO VOICE] Stop failed: {exc}", style="red"))
                return
            self._voice_enabled = False
            self._voice_agent = None
            self._voice_degraded = False
            self._set_voice_status_indicator("OFF", "PTT default")
            chat_log.write(Text("[VOCO VOICE] Stopped.", style="#C96B45"))
            return

        self._set_voice_status_indicator("STARTING", "push-to-talk mode")
        chat_log.write(
            Text(
                f"[VOCO VOICE] Initializing push-to-talk mode + ASR ({constants.VOICE_MODEL_ID})...",
                style="#C96B45",
            )
        )

        try:
            from voice.wake_voice import VocoVoice

            self._voice_agent = VocoVoice(
                on_command_callback=self._queue_voice_command,
                on_status_callback=lambda msg, lvl: self.call_from_thread(self._handle_voice_status_event, msg, lvl),
                interaction_mode=VocoVoice.INTERACTION_MODE_PUSH_TO_TALK,
            )
            self._set_voice_model_indicator(getattr(self._voice_agent, "_whisper_model_size", constants.VOICE_MODEL_ID))
            self._voice_agent.start()
            self._voice_enabled = True
            self._ptt_recording = False
            vad_mode = str(getattr(self._voice_agent, "vad_mode", "")).strip().lower()
            self._voice_degraded = vad_mode not in {"webrtcvad", "ptt-only"}
            if self._voice_degraded:
                self._set_voice_status_indicator("DEGRADED", "PTT degraded", "yellow")
                chat_log.write(
                    Text(
                        "[VOCO VOICE] Started in push-to-talk mode (degraded).",
                        style="yellow",
                    )
                )
                return
            self._set_voice_status_indicator("ON", "PTT", "green")
            chat_log.write(Text("[VOCO VOICE] Push-to-talk ready. Press SPACE to capture speech.", style="#C96B45"))
        except Exception as exc:
            self._voice_agent = None
            self._voice_enabled = False
            self._voice_degraded = True
            self._ptt_recording = False
            self._set_voice_model_indicator("unavailable", "red")
            self._set_voice_status_indicator("DEGRADED", "voice unavailable", "red")
            chat_log.write(Text(f"[VOCO VOICE] Failed to start: {exc}", style="red"))

    def action_ptt_toggle(self) -> None:
        chat_log = self._activate_conversation()
        if not self._voice_enabled or self._voice_agent is None:
            chat_log.write(Text("[VOCO VOICE] Voice is OFF.", style="yellow"))
            return
        if self._ptt_transcribing:
            chat_log.write(Text("[VOCO VOICE] Still transcribing the previous capture...", style="yellow"))
            return

        try:
            if not self._ptt_recording:
                started = bool(self._voice_agent.begin_push_to_talk())
                if not started:
                    chat_log.write(Text("[VOCO VOICE] Push-to-talk start failed.", style="red"))
                    return
                self._ptt_recording = True
                self._set_voice_status_indicator("LISTENING", "PTT recording", "#C96B45")
                chat_log.write(Text("[VOCO VOICE] Recording... press SPACE again to transcribe.", style="#C96B45"))
                return

            self._ptt_recording = False
            self._ptt_transcribing = True
            self._set_voice_status_indicator("PROCESSING", "Transcribing...", "#29B6F6")
            chat_log.write(Text("[VOCO VOICE] Transcribing...", style="#29B6F6"))
            self._start_ptt_transcription()
        except Exception as exc:
            self._ptt_recording = False
            self._set_voice_status_indicator("DEGRADED", "ptt error", "red")
            chat_log.write(Text(f"[VOCO VOICE] Push-to-talk failed: {exc}", style="red"))

    def _start_ptt_transcription(self) -> None:
        if self._voice_agent is None:
            self._finish_ptt_transcription("", "Voice agent is unavailable.")
            return

        def _worker() -> None:
            transcript = ""
            error_message = ""
            try:
                transcript = str(self._voice_agent.end_push_to_talk()).strip()
            except Exception as exc:  # pragma: no cover - runtime-specific failures
                error_message = str(exc)
            self.call_from_thread(self._finish_ptt_transcription, transcript, error_message)

        self._ptt_transcribe_thread = threading.Thread(target=_worker, daemon=True)
        self._ptt_transcribe_thread.start()

    def _finish_ptt_transcription(self, transcript: str, error_message: str) -> None:
        self._ptt_transcribing = False
        chat_log = self._activate_conversation()
        if error_message:
            self._set_voice_status_indicator("DEGRADED", "ptt error", "red")
            chat_log.write(Text(f"[VOCO VOICE] Push-to-talk failed: {error_message}", style="red"))
            return

        if self._voice_degraded:
            self._set_voice_status_indicator("DEGRADED", "PTT degraded", "yellow")
        else:
            self._set_voice_status_indicator("ON", "PTT", "green")

        if not transcript:
            chat_log.write(Text("[VOCO VOICE] No speech captured.", style="yellow"))
            return
        chat_log.write(Text(f"[VOCO VOICE] {transcript}", style="#D4D4D4"))
        self._queue_voice_command(transcript)

    def action_clear_log(self) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.clear()


if __name__ == "__main__":
    VocoApp().run()
