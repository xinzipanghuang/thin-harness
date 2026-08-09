"""Wire bus + channel + agent worker into a runnable terminal chat."""

from __future__ import annotations

import argparse
import asyncio

from agents import AGENT_REGISTRY, create_agent
from core.model import EchoModel
from core.storage import Memory
from rich.console import Console

from .bus import MessageBus
from .channel import TerminalChannel
from .worker import AgentWorker


async def run_chat(
    agent_name: str = "daily",
    demo: bool = False,
    debug_level: int = 0,
    progress: bool = False,
    session: str = "terminal",
    db_path: str = "data/agent.db",
    markdown_live: bool = False,
) -> None:
    model = EchoModel() if demo else None
    if model is None:
        Console().print("[dim]loading model…[/dim]")
    agent = create_agent(agent_name, model=model)
    memory = Memory(db_path)
    bus = MessageBus()
    worker = AgentWorker(bus, agent, memory=memory, session_id=session)
    channel = TerminalChannel(
        bus,
        clear_history=worker.clear_history,
        get_trace=worker.get_trace,
        debug_level=debug_level,
        markdown_live=markdown_live,
        progress=progress,
        tools=[
            {
                "name": tool.name,
                "description": tool.to_schema().get("description", ""),
            }
            for tool in agent.tools
        ],
    )
    bus.subscribe(worker.handle)
    await channel.start()
    await bus.start()
    try:
        await channel.run()
    finally:
        await bus.stop()
        memory.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Terminal chat over the minimal agent runtime")
    parser.add_argument(
        "--agent",
        default="daily",
        choices=sorted(AGENT_REGISTRY),
        help="domain agent subclass to run (model comes from .env)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="offline demo: echo replies, no API key needed",
    )
    parser.add_argument(
        "--debug",
        type=int,
        default=0,
        choices=[0, 1, 2, 3],
        help="debug level: 0 off, 1 steps, 2 + context/thinking, 3 full trace",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="show per-step progress lines and timings (off by default)",
    )
    parser.add_argument(
        "--session",
        default="terminal",
        help="memory session id (default: terminal; persists across restarts)",
    )
    parser.add_argument(
        "--db",
        default="data/agent.db",
        help="SQLite database path for memory (default: data/agent.db)",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="render streamed answers as Markdown live (needs an ANSI-capable terminal; "
        "off by default because full-buffer redraw can duplicate text on some terminals)",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            run_chat(
                agent_name=args.agent,
                demo=args.demo,
                debug_level=args.debug,
                progress=args.progress,
                session=args.session,
                db_path=args.db,
                markdown_live=args.markdown,
            )
        )
    except KeyboardInterrupt:
        # Ctrl+C during chat: exit cleanly like /quit, no traceback.
        Console().print("\n[dim]bye - see you next time[/dim]")


if __name__ == "__main__":
    main()
