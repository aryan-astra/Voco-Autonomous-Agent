"""VOCO TUI frontend prototype using Textual (no backend logic)."""

import asyncio
import threading
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Input, Label, RichLog, Rule, Static

from context import AgentContext
from orchestrator import run as orchestrator_run


_agent_context = AgentContext()


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
                            yield Label("Qwen 2.5 Coder • Local LLM", classes="meta")
                            yield Label("/workspace/voco/apps", classes="meta")

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
        chat_log.write(Text("  Ctrl+V            Mock voice input", style="#D4D4D4"))

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

        if normalized == "/back":
            chat_log.write(Text("[VOCO] Already on home dashboard.", style="#C96B45"))
            return True

        if normalized == "/workspace":
            await asyncio.sleep(0.15)
            chat_log.write(Text("[VOCO] Workspace", style="#C96B45"))
            chat_log.write(Text("  Root   /workspace/voco/apps", style="#D4D4D4"))
            chat_log.write(Text("  Model  Qwen 2.5 Coder • Local LLM", style="#D4D4D4"))
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

    def _update_output(self, formatted: str) -> None:
        """Write a formatted status line to the output panel."""
        chat_log = self._activate_conversation()
        chat_log.write(formatted)

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
        formatted = f"[{color}]{message}[/{color}]"
        self.call_from_thread(self._update_output, formatted)

    def _handle_user_input(self, task: str) -> None:
        """Run orchestrator task in a background thread to keep the TUI responsive."""
        if not task.strip():
            return

        def run_task() -> None:
            try:
                orchestrator_run(
                    task=task,
                    context=_agent_context,
                    ui_callback=lambda msg, lvl: self._emit_to_ui(msg, lvl),
                )
            except Exception as exc:
                self._emit_to_ui(f"[VOCO] FATAL ERROR: {exc}", "error")

        thread = threading.Thread(target=run_task, daemon=True)
        thread.start()

    def action_voice_toggle(self) -> None:
        # Ctrl+V now works from both dashboard and conversation views.
        chat_log = self._activate_conversation()
        chat_log.write(Text("[VOCO VOICE] Listening...", style="#C96B45"))


if __name__ == "__main__":
    VocoApp().run()
