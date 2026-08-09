"""The Agent base class: subclass it to define a domain agent.

Agent = Model + System Prompt + Selected Tools + Harness Policy.

Instead of YAML, agents are Python subclasses. A domain agent only overrides
plain class attributes (prompt, tools, runtime limits); the model is built
from .env by the provider layer — see ``agents/`` for ready-made examples:

    from agents import CodingAgent

    agent = CodingAgent()
    result = await agent.run("Inspect this project.")
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .context import ContextConfig, environment_info
from .model import Model
from .providers import resolve
from .tool import Tool, ToolContext, select_tools
from .types import Observation, RuntimeConfig, RunResult, ToolCall, ToolResult

BootstrapHook = Callable[[ToolContext], Awaitable[list[Observation]]]

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_prompt(path: str) -> str:
    return (_PROJECT_ROOT / path).read_text(encoding="utf-8")


class Agent:
    """Base class for domain agents.

    Subclasses override the class attributes below; the constructor assembles
    the model, system prompt, tool selection, and harness policy from them.
    Explicit constructor arguments (model, tools, runtime, ...) take
    precedence, which is what tests and offline demos use.
    """

    name: str = "agent"
    prompt: str = ""  # inline system prompt
    prompt_path: str = ""  # or path to a prompt file, relative to the project root
    prompt_paths: list[str] = []  # optional composable base + domain prompts
    tool_include: list[str] = ["*"]
    tool_exclude: list[str] = []
    tool_packages: list[str] = ["tools"]
    own_tools: list = []  # agent-private tools (Tool instances or functions)
    max_steps: int = 8
    max_tool_calls: int = 12
    tool_timeout: float = 60.0
    max_consecutive_failures: int = 3
    max_consecutive_no_gain: int = 2
    model_timeout: float = 90.0
    request_timeout: float = 300.0
    final_regenerate: bool = True
    experience_enabled: bool = True  # use the experience/evolution module
    workdir: Optional[str] = None
    log_dir: Optional[str] = None
    max_tool_result_chars: int = 8000
    keep_recent_observations: int = 5

    def __init__(
        self,
        model: Optional[Model] = None,
        *,
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        runtime: Optional[RuntimeConfig] = None,
        context: Optional[ContextConfig] = None,
        workdir: Optional[str] = None,
        log_dir: Optional[str] = None,
        bootstrap: Optional[BootstrapHook] = None,
        memory=None,
        session_id: str = "",
    ) -> None:
        self.name = name or self.name
        # The model is constructed from .env (LLM_API_KEY / LLM_BASE_URL /
        # LLM_MODEL / LLM_ENABLE_THINKING) by the provider layer — an agent
        # does not configure a model itself.
        self.model = model if model is not None else resolve({})
        prompt = system_prompt
        if prompt is None:
            prompt = self.prompt
        if not prompt and self.prompt_paths:
            prompt = "\n\n".join(_read_prompt(path) for path in self.prompt_paths)
        elif not prompt and self.prompt_path:
            prompt = _read_prompt(self.prompt_path)
        self.system_prompt = prompt or ""
        private_tools = self._private_tools(self.own_tools)
        if tools is not None:
            self.tools = list(tools) + private_tools
        else:
            self.tools = select_tools(
                {"include": list(self.tool_include), "exclude": list(self.tool_exclude)},
                package_names=list(self.tool_packages),
            ) + private_tools
        self.runtime = runtime or RuntimeConfig(
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            tool_timeout=self.tool_timeout,
            max_consecutive_failures=self.max_consecutive_failures,
            max_consecutive_no_gain=self.max_consecutive_no_gain,
            model_timeout=self.model_timeout,
            request_timeout=self.request_timeout,
            final_regenerate=self.final_regenerate,
            experience_enabled=self.experience_enabled,
            workdir=workdir if workdir is not None else self.workdir,
            log_dir=log_dir if log_dir is not None else self.log_dir,
        )
        self.context = context or ContextConfig(
            max_tool_result_chars=self.max_tool_result_chars,
            keep_recent_observations=self.keep_recent_observations,
        )
        self.bootstrap = bootstrap if bootstrap is not None else self.bootstrap
        self.memory = memory
        self.session_id = session_id
        # Time state: the agent always knows its own clock. ``started_at`` is
        # when this agent instance (chat session) was created; ``last_run_at``
        # is refreshed at the start of every run.
        self.started_at = datetime.now().astimezone()
        self.last_run_at: Optional[datetime] = None
        # Environment: OS / Python / terminal / shell / user / cwd, so hooks
        # and tools can adapt without re-detecting it themselves.
        self.environment: dict[str, str] = environment_info()

    def now(self) -> datetime:
        """Current local time, so hooks/tools can read the agent's clock."""
        return datetime.now().astimezone()

    @staticmethod
    def _private_tools(items) -> list[Tool]:
        """Normalize agent-private tools (Tool instances or plain functions)."""
        result: list[Tool] = []
        for item in items or []:
            if isinstance(item, Tool):
                result.append(item)
            else:
                module = item.__module__.split(".")[-1]
                result.append(Tool(name=f"{module}.{item.__name__}", fn=item))
        return result

    async def on_run_start(self, request: str) -> None:
        """Override: called once before the loop starts (e.g. setup)."""

    async def bootstrap(self, ctx: ToolContext) -> list[Observation]:
        """Override: cheap deterministic pre-model behavior (optional).

        Runs before the first model call; may inspect the environment and
        return observations (or record facts via ``ctx.record_fact``).
        """
        return []

    async def on_tool_result(
        self,
        ctx: ToolContext,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Override: react to every tool result (e.g. record facts)."""

    async def finalize(self, ctx: ToolContext, text: str, state) -> str:
        """Override: deterministically validate or format the final output."""
        return text

    async def run(
        self,
        request: str,
        history: Optional[list[tuple[str, str]]] = None,
        on_token=None,
        on_debug=None,
        memory=None,
        session_id: str = "",
    ) -> RunResult:
        from .loop import run_agent

        return await run_agent(
            self,
            request,
            history=history,
            on_token=on_token,
            on_debug=on_debug,
            memory=memory if memory is not None else self.memory,
            session_id=session_id or self.session_id,
        )
