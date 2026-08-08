"""TerminalChannel: rich-rendered input/output for direct terminal chat."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unicodedata
import uuid
from typing import Callable, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .bus import MessageBus
from .events import InboundMessage, OutboundEvent

HELP_TEXT = """\
Commands:
  /help     show this help
  /clear    clear conversation history
  /tools    list available tools
  /exit     leave the chat
  Ctrl+C    exit (same as /quit)
  Shift+Enter  insert a newline in a message

Type a message and press Enter to talk to the agent.
"""

_INPUT_PROMPT = "[bold cyan]you[/bold cyan] > "


def _display_width(ch: str) -> int:
    """Terminal column width of one character (CJK wide chars = 2)."""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _read_line_blocking(console: Console) -> str:
    """Read one message, blocking.

    On Windows, raw console input is used so Enter submits and Shift+Enter
    inserts a newline (multi-line messages). Falls back to plain single-line
    ``input()`` on other platforms or if the console API is unavailable.
    """
    if sys.platform == "win32":
        try:
            return _read_win32_console(console)
        except Exception:
            pass
    return console.input(_INPUT_PROMPT)


def _read_win32_console(console: Console) -> str:
    """Windows console line input via ReadConsoleInputW.

    Enter (no shift) submits the message; Shift+Enter appends a newline so the
    message can span several lines. Backspace is width-aware (CJK wide
    characters are erased across both columns) and can cross inserted
    newlines; Ctrl+C raises KeyboardInterrupt (same as /quit).
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    STD_INPUT_HANDLE = -10
    KEY_EVENT = 0x0001
    VK_RETURN = 0x0D
    VK_BACK = 0x08
    SHIFT_PRESSED = 0x0010
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004

    class KeyEventRecord(ctypes.Structure):
        _fields_ = [
            ("b_key_down", wintypes.BOOL),
            ("repeat_count", wintypes.WORD),
            ("virtual_key_code", wintypes.WORD),
            ("virtual_scan_code", wintypes.WORD),
            ("unicode_char", wintypes.WCHAR),
            ("control_key_state", wintypes.DWORD),
        ]

    class InputRecord(ctypes.Structure):
        _fields_ = [("event_type", wintypes.WORD), ("key_event", KeyEventRecord)]

    handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        raise OSError("GetConsoleMode failed")
    try:
        kernel32.SetConsoleMode(
            handle, mode.value & ~ENABLE_LINE_INPUT & ~ENABLE_ECHO_INPUT
        )
        console.print(_INPUT_PROMPT, end="")
        sys.stdout.flush()
        buffer: list[str] = []
        record = InputRecord()
        read_count = wintypes.DWORD()
        while True:
            if not kernel32.ReadConsoleInputW(
                handle, ctypes.byref(record), 1, ctypes.byref(read_count)
            ):
                break
            if not read_count.value or record.event_type != KEY_EVENT:
                continue
            key = record.key_event
            if not key.b_key_down:
                continue
            if key.virtual_key_code == VK_RETURN:
                if key.control_key_state & SHIFT_PRESSED:
                    buffer.append("\n")
                    sys.stdout.write("\n")
                else:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    break
            elif key.virtual_key_code == VK_BACK:
                if buffer:
                    removed = buffer.pop()
                    if removed == "\n":
                        sys.stdout.write("\x1b[1A\x1b[2K")
                    else:
                        width = _display_width(removed)
                        sys.stdout.write("\b" * width + " " * width + "\b" * width)
                    sys.stdout.flush()
            elif key.unicode_char and key.unicode_char != "\x00":
                ch = key.unicode_char
                if ch == "\x03":  # Ctrl+C: behave like /quit
                    raise KeyboardInterrupt
                buffer.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        return "".join(buffer)
    finally:
        kernel32.SetConsoleMode(handle, mode.value)

class TerminalChannel:
    def __init__(
        self,
        bus: MessageBus,
        console: Optional[Console] = None,
        clear_history: Optional[Callable[[], None]] = None,
        debug_level: int = 0,
        markdown_live: bool = False,
        progress: bool = False,
        tools: Optional[list[str]] = None,
    ) -> None:
        self.bus = bus
        self.console = console or Console()
        self.clear_history = clear_history
        self.debug_level = debug_level
        self.markdown_live = markdown_live
        self.progress = progress
        self.tools = sorted(
            (
                {"name": tool, "description": ""}
                if isinstance(tool, str)
                else dict(tool)
                for tool in (tools or [])
            ),
            key=lambda tool: tool["name"],
        )
        self._pending: dict[str, asyncio.Future[OutboundEvent]] = {}
        self._streamed: set[str] = set()
        self._live: dict[str, Live] = {}
        self._stream_buffers: dict[str, list[str]] = {}
        self._stream_partial: dict[str, str] = {}
        self._buffered_debug: list[OutboundEvent] = []
        self._timing: dict[str, dict[str, float]] = {}
        self._timers: dict[str, dict] = {}
        self._last_elapsed_s: Optional[float] = None

    async def start(self) -> None:
        self.bus.subscribe_outbound(self._on_outbound)

    async def _on_outbound(self, event: OutboundEvent) -> None:
        if event.kind == "stream":
            # The answer is streaming: finalize the in-place ticker line first
            # so the ticker and the streamed content never fight over a row.
            self._stop_timer(event.reply_to)
            self._streamed.add(event.reply_to)
            text = event.text or ""
            buffer = self._stream_buffers.setdefault(event.reply_to, [])
            buffer.append(text)
            live = self._live.get(event.reply_to)
            if live is None and self.markdown_live and self.console.is_terminal:
                live = Live(
                    Markdown(""),
                    console=self.console,
                    refresh_per_second=12,
                    vertical_overflow="visible",
                )
                self._live[event.reply_to] = live
                live.start()
            if live is not None:
                live.update(Markdown("".join(buffer)))
            else:
                # default: forward-only line-by-line Markdown (never redraws,
                # so it cannot duplicate, and works on any terminal)
                self._append_stream_lines(event.reply_to, text)
            return
        if event.kind == "debug":
            # Flush any buffered streamed line before a new model call so a
            # short preamble (no trailing newline) is not glued onto the next
            # answer, and progress lines are not preceded by blank lines.
            if (
                (event.detail or {}).get("kind") == "model_call_start"
                and event.reply_to in self._streamed
            ):
                pending = self._stream_partial.pop(event.reply_to, "")
                if pending.strip():
                    self.console.print(Markdown(pending), soft_wrap=True)
            if self._live:
                self._buffered_debug.append(event)
            else:
                self._render_debug_event(event.detail, event.reply_to)
            return
        if event.kind == "status":
            if event.reply_to in self._streamed:
                self.console.print()
            self._render(event)
            return
        if event.kind not in ("reply", "error"):
            return
        future = self._pending.get(event.reply_to)
        if future is not None and not future.done():
            future.set_result(event)

    async def run(self) -> None:
        console = self.console
        console.print(
            Panel.fit("[bold]minimal agent chat[/bold] — type a message, or /help", border_style="cyan")
        )
        while True:
            try:
                raw = await self._read_line()
            except EOFError:
                break
            except KeyboardInterrupt:
                # Ctrl+C is treated as /quit (clean exit, no traceback).
                console.print()
                break
            text = raw.strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/help":
                console.print(HELP_TEXT)
                continue
            if text == "/clear":
                if self.clear_history is not None:
                    self.clear_history()
                console.print("[dim]conversation history cleared[/dim]")
                continue
            if text == "/tools":
                if self.tools:
                    table = Table(
                        title="Available tools",
                        header_style="bold",
                        expand=True,
                    )
                    table.add_column("Tool", no_wrap=True)
                    table.add_column("Purpose")
                    for tool in self.tools:
                        table.add_row(tool["name"], tool.get("description") or "")
                    console.print(table)
                else:
                    console.print("[dim]no tools available[/dim]")
                continue

            message = InboundMessage(id=uuid.uuid4().hex[:12], channel="terminal", content=text)
            future = asyncio.get_running_loop().create_future()
            self._pending[message.id] = future
            await self.bus.publish(message)
            event = await future
            self._pending.pop(message.id, None)
            self._render(event)

    async def _read_line(self) -> str:
        """Read one terminal line on a daemon thread.

        Ctrl+C must behave like /quit. The input thread may still be blocked
        in ``input()`` when we exit, but daemon threads are not joined at
        interpreter exit, so the process shuts down cleanly instead of waiting
        for a stuck thread (asyncio.to_thread would hang at shutdown).
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def _set_result(value: str) -> None:
            if not future.done():
                future.set_result(value)

        def _set_exception(exc: BaseException) -> None:
            if not future.done():
                future.set_exception(exc)

        def _deliver(callback, value) -> None:
            try:
                loop.call_soon_threadsafe(callback, value)
            except RuntimeError:
                pass  # loop already closed; the process is exiting anyway

        def _read() -> None:
            try:
                line = _read_line_blocking(self.console)
            except BaseException as exc:  # EOFError / KeyboardInterrupt in the thread
                _deliver(_set_exception, exc)
            else:
                _deliver(_set_result, line)

        threading.Thread(target=_read, daemon=True, name="chat-input").start()
        return await future

    def _render(self, event: OutboundEvent) -> None:
        console = self.console
        self._stop_timer(event.reply_to)
        if event.kind == "reply":
            if event.reply_to in self._streamed:
                live = self._live.pop(event.reply_to, None)
                text = "".join(self._stream_buffers.pop(event.reply_to, []))
                if live is not None:
                    live.update(Markdown(text))
                    live.stop()
                else:
                    tail = self._stream_partial.pop(event.reply_to, "")
                    if tail.strip():
                        console.print(Markdown(tail), soft_wrap=True)
                    console.print()
                self._flush_debug()
                detail = event.detail or {}
                bits = [f"{detail.get('steps', 0)} steps", f"{detail.get('tool_calls', 0)} tool calls"]
                if detail.get("failures"):
                    bits.append(f"{detail['failures']} failures")
                if detail.get("stop_reason") and detail["stop_reason"] != "completed":
                    bits.append(f"stopped: {detail['stop_reason']}")
                self._print_summary(bits)
                self._streamed.discard(event.reply_to)
            else:
                console.print(
                    Panel(
                        Markdown(event.text or ""),
                        title="[bold green]agent[/bold green]",
                        border_style="green",
                    )
                )
                detail = event.detail or {}
                bits = [f"{detail.get('steps', 0)} steps", f"{detail.get('tool_calls', 0)} tool calls"]
                if detail.get("failures"):
                    bits.append(f"{detail['failures']} failures")
                if detail.get("stop_reason") and detail["stop_reason"] != "completed":
                    bits.append(f"stopped: {detail['stop_reason']}")
                self._print_summary(bits)
        elif event.kind == "status":
            console.print(f"[dim]{event.text}[/dim]")
        elif event.kind == "error":
            if event.reply_to in self._streamed:
                live = self._live.pop(event.reply_to, None)
                if live is not None:
                    live.stop()
                else:
                    self._stream_partial.pop(event.reply_to, None)
                    console.print()
                self._stream_buffers.pop(event.reply_to, None)
                self._flush_debug()
                self._streamed.discard(event.reply_to)
            console.print(f"[bold red]error:[/bold red] {event.text}")

    def _append_stream_lines(self, reply_to: str, text: str) -> None:
        """Render complete Markdown lines forward-only (no redraw, no duplicates)."""
        combined = self._stream_partial.get(reply_to, "") + text
        parts = combined.split("\n")
        # A chunk ending with "\n" terminates the current line; the trailing
        # empty part is the terminator, not a blank line to render.
        if combined.endswith("\n"):
            complete, tail = parts[:-1], ""
        else:
            complete, tail = parts[:-1], parts[-1]
        for line in complete:
            if line.strip():
                self.console.print(Markdown(line), soft_wrap=True)
            else:
                self.console.print()
        self._stream_partial[reply_to] = tail

    def _flush_debug(self) -> None:
        pending, self._buffered_debug = self._buffered_debug, []
        for event in pending:
            self._render_debug_event(event.detail, event.reply_to)

    def _render_debug_event(self, detail: dict, reply_to: str) -> None:
        level = int(detail.get("level", 0))
        kind = detail.get("kind", "")
        if kind == "model_call_start" and self.console.is_terminal and self.progress:
            self._start_timer(reply_to, detail)
        elif kind in ("model_response", "final"):
            self._stop_timer(reply_to)
        timing = self._timing.setdefault(reply_to, {"model_ms": 0.0, "tool_ms": 0.0})
        if kind == "model_response":
            timing["model_ms"] += float(detail.get("duration_ms") or 0)
        elif kind == "tool_call_end":
            timing["tool_ms"] += float(detail.get("duration_ms") or 0)
        if kind == "final":
            self._last_elapsed_s = float(detail.get("elapsed_ms") or 0) / 1000
            self._timing.pop(reply_to, None)
        if level == 1 and self.debug_level == 0 and self.progress:
            if kind == "model_call_start" and self.console.is_terminal:
                pass  # the in-place ticker already shows this line
            else:
                self._render_progress(detail)
        elif level <= self.debug_level:
            self._render_debug(detail)

    def _start_timer(self, reply_to: str, detail: dict) -> None:
        """An in-place ticking status line (no Rich Live: \r rewrites only)."""
        self._stop_timer(reply_to)
        label = (
            "polishing final answer…"
            if detail.get("phase") == "final"
            else "calling model…"
        )
        step = detail.get("step")
        max_steps = detail.get("max_steps")
        base = float(detail.get("elapsed_ms") or 0) / 1000
        stop_event = asyncio.Event()
        entry = {
            "event": stop_event,
            "label": label,
            "step": step,
            "max_steps": max_steps,
            "base": base,
            "started": time.monotonic(),
        }
        self._timers[reply_to] = entry

        async def tick() -> None:
            while self._timers.get(reply_to) is entry:
                elapsed = entry["base"] + (time.monotonic() - entry["started"])
                sys.stdout.write(
                    "\r"
                    + f"[t={elapsed:.1f}s]  step {entry['step']}/{entry['max_steps']} "
                    + f"— {entry['label']}"
                )
                sys.stdout.flush()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                break

        asyncio.create_task(tick())

    def _stop_timer(self, reply_to: str) -> None:
        entry = self._timers.pop(reply_to, None)
        if entry is not None:
            entry["event"].set()
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _render_progress(self, detail: dict) -> None:
        """Compact always-on progress lines so the terminal never looks frozen."""
        console = self.console
        kind = detail.get("kind", "")
        elapsed = float(detail.get("elapsed_ms") or 0) / 1000
        tag = f"\\[t={elapsed:.1f}s]"
        if kind == "model_call_start":
            label = (
                "polishing final answer…"
                if detail.get("phase") == "final"
                else "calling model…"
            )
            console.print(
                f"[dim]{tag} step {detail.get('step')}/{detail.get('max_steps')} — {label}[/dim]"
            )
        elif kind == "model_response":
            calls = detail.get("tool_calls") or []
            action = f"calling {', '.join(calls)}" if calls else "answering…"
            duration = float(detail.get("duration_ms") or 0) / 1000
            console.print(f"[dim]{tag} step {detail.get('step')}: {action} ({duration:.1f}s)[/dim]")
        elif kind == "tool_call_start":
            args = json.dumps(detail.get("arguments", {}), ensure_ascii=False, default=str)
            console.print(f"[dim]{tag}  → {detail.get('name')}({args[:100]})[/dim]")
        elif kind == "tool_call_end":
            name = detail.get("name")
            if detail.get("ok"):
                console.print(
                    f"[dim]{tag}  ✓ {name} ({detail.get('duration_ms')}ms)[/dim]"
                )
            else:
                reason = (detail.get("error") or "")[:80]
                console.print(f"[dim]{tag}  ✗ {name}: {reason}[/dim]")

    def _render_debug(self, detail: dict) -> None:
        console = self.console
        level = int(detail.get("level", 1))
        kind = detail.get("kind", "")
        tag = "[cyan]debug[/cyan]"
        if kind == "run_start":
            console.print(f"{tag} run start: {detail.get('agent')} — {detail.get('request')}")
        elif kind == "bootstrap":
            console.print(f"{tag} bootstrap: {detail.get('observations')} observations, {detail.get('facts')} facts")
        elif kind == "context":
            parts = [
                f"{message.get('role')} {message.get('chars')} chars"
                for message in detail.get("messages", [])
            ]
            console.print(f"{tag} context: {', '.join(parts)}")
            if level >= 3:
                for message in detail.get("messages", []):
                    console.print(f"{tag} --- {message.get('role')} ---")
                    console.print((message.get("content") or "")[:2000])
        elif kind == "model_response":
            duration = float(detail.get("duration_ms") or 0) / 1000
            console.print(
                f"{tag} model step {detail.get('step')}: usage={detail.get('usage')} "
                f"calls={detail.get('tool_calls')} ({duration:.1f}s)"
            )
        elif kind == "model_thinking":
            console.print(f"{tag} thinking: {(detail.get('thinking') or '')[:600]}")
        elif kind == "tool_call_start":
            args = json.dumps(detail.get("arguments", {}), ensure_ascii=False, default=str)
            console.print(f"{tag} tool → {detail.get('name')}({args})")
        elif kind == "tool_call_end":
            console.print(
                f"{tag} tool ← {detail.get('name')} ok={detail.get('ok')} "
                f"{detail.get('duration_ms')}ms error={detail.get('error')}"
            )
        elif kind == "final":
            console.print(f"{tag} final: {detail.get('stop_reason')}")

    def _print_summary(self, bits: list[str]) -> None:
        prefix = (
            f"\\[t={self._last_elapsed_s:.1f}s] "
            if self._last_elapsed_s is not None
            else ""
        )
        self.console.print("[dim]" + prefix + " · ".join(bits) + "[/dim]")
