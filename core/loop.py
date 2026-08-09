"""The core agent loop: model -> tool calls -> environment -> observations."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .agent import Agent
from .artifacts import ArtifactStore
from .context import ContextBuilder
from .tool import Tool, ToolContext, execute_tool, serialize_output, wire_name
from .types import (
    Artifact,
    DebugEvent,
    Evidence,
    Fact,
    Message,
    Observation,
    RunResult,
    RunState,
    ToolCall,
    ToolResult,
    new_run_id,
    utcnow,
)


@dataclass
class RunLog:
    """Structured, inspectable record of a run (never sent to the model)."""

    run_id: str
    agent: str = ""
    entries: list[dict] = field(default_factory=list)

    def record(self, event: str, **fields) -> None:
        self.entries.append({"ts": utcnow(), "run_id": self.run_id, "event": event, **fields})


async def run_agent(
    agent: Agent,
    request: str,
    history: Optional[list[tuple[str, str]]] = None,
    on_token: Optional[Callable[[str], Awaitable[None]]] = None,
    on_debug: Optional[Callable[[int, str, dict], Awaitable[None]]] = None,
    memory=None,
    session_id: str = "",
) -> RunResult:
    """Run the loop until the model answers or a deterministic guard fires."""
    run_id = new_run_id()
    log = RunLog(run_id=run_id, agent=agent.name)
    log.record("run_start", request=request, tools=[t.name for t in agent.tools])
    run_started = time.perf_counter()
    agent.last_run_at = datetime.now().astimezone()

    store = ArtifactStore()
    ctx = ToolContext(
        artifact_store=store,
        workdir=agent.runtime.workdir,
        request=request,
        memory=memory,
        session_id=session_id,
        environment=agent.environment,
    )
    state = RunState(request=request)
    ctx.facts = state.facts
    fact_cursor = 0
    persisted_fact_cursor = 0
    debug_events: list[DebugEvent] = []

    # Codex-style memory: load prior turns, verified facts and reusable
    # experiences ("evolution") into context.
    experiences: list[dict] = []
    session_times: Optional[tuple] = None
    if memory is not None and session_id:
        try:
            session_times = memory.session_times(session_id)
            if history is None:
                history = memory.load_history(
                    session_id,
                    limit=(
                        agent.context.history_recent_turns
                        + agent.context.history_summary_turns
                    ),
                    numbered=True,
                )
            for fact in memory.load_facts(session_id, limit=agent.context.max_facts):
                state.facts.append(
                    Fact(
                        id=f"mem-{fact.id}",
                        value=fact.value,
                        source=fact.source,
                        tool=fact.tool,
                    )
                )
            fact_cursor = len(state.facts)
            persisted_fact_cursor = len(state.facts)
            if agent.runtime.experience_enabled:
                # Keep injected experiences to a couple of clearly-relevant
                # hints; too many would bias the model toward old paths over
                # exploring.
                experiences = memory.search_experiences(
                    request,
                    limit=2,
                    stale_after_days=agent.context.experience_stale_days,
                )
        except Exception as exc:
            log.record("memory_error", error=f"{type(exc).__name__}: {exc}")

    async def debug(level: int, kind: str, **detail) -> None:
        event = DebugEvent(
            level=level,
            kind=kind,
            detail={
                **detail,
                "elapsed_ms": round((time.perf_counter() - run_started) * 1000, 1),
            },
        )
        debug_events.append(event)
        if on_debug is None:
            return
        try:
            await on_debug(event.level, event.kind, dict(event.detail))
        except Exception as exc:
            log.record("debug_error", error=f"{type(exc).__name__}: {exc}")

    await debug(1, "run_start", agent=agent.name, request=request)

    try:
        await agent.on_run_start(request)
    except Exception as exc:
        log.record("hook_error", hook="on_run_start", error=f"{type(exc).__name__}: {exc}")

    # Model/API-facing tool names must match ^[a-zA-Z0-9_-]+$ (dots -> "_");
    # they are mapped back to registry names before execution.
    tool_wire_names = {tool.name: wire_name(tool.name) for tool in agent.tools}
    reverse_names = {wire: original for original, wire in tool_wire_names.items()}
    model_tools = [
        {**tool.to_schema(), "name": tool_wire_names[tool.name]} for tool in agent.tools
    ]
    builder = ContextBuilder(
        agent.system_prompt,
        agent.context,
        [wire_name(tool.name) for tool in agent.tools],
    )
    # Append-only message history (Codex-style): the base is built once and
    # every change is appended as new messages, keeping the prefix byte-stable.
    messages = builder.build_initial(
        state,
        history=history,
        experiences=experiences,
        session_times=session_times,
    )

    if agent.bootstrap is not None:
        try:
            observations = await agent.bootstrap(ctx)
            for obs in observations or []:
                obs.step = 0
                state.observations.append(obs)
            if observations:
                messages.append(builder.bootstrap_message(observations))
            log.record("bootstrap", observations=len(state.observations), facts=len(state.facts))
            await debug(1, "bootstrap", observations=len(state.observations), facts=len(state.facts))
        except Exception as exc:
            state.harness_notices.append(f"Bootstrap hook failed: {type(exc).__name__}: {exc}")
            messages.append(
                builder.notice_message(
                    f"Bootstrap hook failed: {type(exc).__name__}: {exc}"
                )
            )
            log.record("bootstrap_error", error=str(exc))

    stop_reason = "completed"
    no_gain = 0
    try:
        async def _drive() -> None:
            nonlocal stop_reason, no_gain, fact_cursor
            while True:
                if state.steps >= agent.runtime.max_steps:
                    stop_reason = "max_steps"
                    break
                if state.tool_calls >= agent.runtime.max_tool_calls:
                    stop_reason = "max_tool_calls"
                    break
                if state.consecutive_failures >= agent.runtime.max_consecutive_failures:
                    stop_reason = "consecutive_failures"
                    break
                if no_gain >= agent.runtime.max_consecutive_no_gain:
                    stop_reason = "no_gain"
                    break

                if not state.stop_hint_sent and builder.stop_hint_due(state):
                    messages.append(builder.stop_hint_message())
                    state.stop_hint_sent = True
                estimated = builder.estimate_tokens(messages)
                state.last_estimated_tokens = estimated
                budget = agent.context.token_budget_tokens
                await debug(
                    1,
                    "context_tokens",
                    tokens=estimated,
                    budget=budget or None,
                )
                if budget and estimated >= budget and not state.stop_hint_sent:
                    messages.append(builder.stop_hint_message())
                    state.stop_hint_sent = True
                await debug(
                    2,
                    "context",
                    messages=[
                        {
                            "role": message.role,
                            "chars": len(message.content),
                            "tool_calls": len(message.tool_calls or []),
                            "content": message.content,
                        }
                        for message in messages
                    ],
                )
                await debug(
                    1,
                    "model_call_start",
                    step=state.steps + 1,
                    max_steps=agent.runtime.max_steps,
                )
                call_started = time.perf_counter()
                try:
                    # With final_regenerate on and tools already used, the draft
                    # answer is not streamed — the polished final pass below is
                    # what the user sees. Tool-free runs stream normally.
                    call_on_token = (
                        None
                        if agent.runtime.final_regenerate and state.tool_calls > 0
                        else on_token
                    )
                    response = await asyncio.wait_for(
                        agent.model.respond(
                            list(messages), model_tools, on_token=call_on_token
                        ),
                        timeout=agent.runtime.model_timeout,
                    )
                except asyncio.TimeoutError:
                    stop_reason = "error"
                    state.harness_notices.append(
                        f"Model call timed out after {agent.runtime.model_timeout}s"
                    )
                    log.record("model_timeout", timeout=agent.runtime.model_timeout)
                    break
                state.steps += 1
                await debug(
                    1,
                    "model_response",
                    step=state.steps,
                    usage=response.usage,
                    tool_calls=[call.name for call in response.tool_calls],
                    metrics=response.metrics,
                    duration_ms=round((time.perf_counter() - call_started) * 1000, 1),
                )
                if response.thinking:
                    await debug(2, "model_thinking", thinking=response.thinking)
                log.record(
                    "model_call",
                    step=state.steps,
                    usage=response.usage,
                    tool_calls=[c.name for c in response.tool_calls],
                )

                if response.is_final:
                    state.final_text = response.text or ""
                    break

                remaining = agent.runtime.max_tool_calls - state.tool_calls
                if remaining <= 0:
                    stop_reason = "max_tool_calls"
                    break
                raw_calls = response.tool_calls[:remaining]
                for index, call in enumerate(raw_calls):
                    if not call.id:
                        call.id = f"call_{state.steps}_{index}"
                # Echo the model's calls (wire names) back into history before
                # mapping them to registry names for execution.
                messages.append(builder.assistant_tool_calls_message(raw_calls))
                calls = [
                    ToolCall(
                        name=reverse_names.get(call.name, call.name),
                        arguments=call.arguments,
                        id=call.id,
                    )
                    for call in raw_calls
                ]
                budget_exhausted = len(calls) < len(response.tool_calls)
                observations_before = len(state.observations)
                artifacts_before = len(state.artifacts)
                facts_before = len(state.facts)
                recorded = await _execute_calls(agent, ctx, state, calls, log, debug)
                for call, obs in zip(calls, recorded):
                    messages.append(builder.tool_result_message(call, obs))
                if len(state.facts) > fact_cursor:
                    messages.append(builder.facts_message(state.facts[fact_cursor:]))
                    fact_cursor = len(state.facts)
                gain = (
                    sum(
                        1
                        for obs in state.observations[observations_before:]
                        if obs.status == "success"
                    )
                    + (len(state.artifacts) - artifacts_before)
                    + (len(state.facts) - facts_before)
                )
                no_gain = no_gain + 1 if gain == 0 else 0
                if no_gain == 1:
                    messages.append(
                        builder.notice_message(
                            "No new evidence in the last tool round. If you are "
                            "reusing a historical experience, check whether it "
                            "still applies; consider exploring an alternative "
                            "tool or path."
                        )
                    )
                if budget_exhausted:
                    stop_reason = "max_tool_calls"
                    break

        await asyncio.wait_for(_drive(), timeout=agent.runtime.request_timeout)
    except asyncio.TimeoutError:
        stop_reason = "timeout"
        state.harness_notices.append(f"Run timed out after {agent.runtime.request_timeout}s")
        log.record("run_timeout", timeout=agent.runtime.request_timeout)
    except Exception as exc:
        stop_reason = "error"
        state.harness_notices.append(f"Harness error: {type(exc).__name__}: {exc}")
        log.record("run_error", error=f"{type(exc).__name__}: {exc}")

    if stop_reason != "completed":
        state.final_text = _guard_message(state, stop_reason)
    await debug(1, "final", stop_reason=stop_reason)
    if (
        agent.runtime.final_regenerate
        and stop_reason not in ("error", "timeout")
        and (stop_reason != "completed" or state.tool_calls > 0)
    ):
        fallback = state.final_text or ""
        final_text = await _final_generation(
            agent, state, request, history, on_token, log, debug
        )
        if final_text:
            state.final_text = final_text
            log.record("final_generation", text=final_text)
        else:
            state.final_text = fallback
    if memory is not None and session_id:
        try:
            memory.save_run(
                session_id=session_id,
                run_id=run_id,
                request=request,
                response=state.final_text or "",
                stop_reason=stop_reason,
                steps=state.steps,
                tool_calls=state.tool_calls,
                failures=state.failures,
                facts=state.facts[persisted_fact_cursor:],
                artifacts=state.artifacts,
                artifact_store=store,
                debug_events=debug_events,
            )
            # Evolution: distill this run into one reusable experience and
            # store it as JSON (methodology, not results).
            if (
                agent.runtime.experience_enabled
                and state.tool_calls > 0
                and stop_reason not in ("error", "timeout")
            ):
                experience = await _reflect_experience(
                    agent, state, request, stop_reason, log, debug
                )
                if experience:
                    exp_id = memory.save_experience(
                        experience, session_id=session_id, turn_id=run_id
                    )
                    log.record(
                        "experience_recorded",
                        id=exp_id,
                        problem_type=experience.get("problem_type", ""),
                    )
        except Exception as exc:
            log.record("memory_error", error=f"{type(exc).__name__}: {exc}")
    log.record("run_end", stop_reason=stop_reason, text=state.final_text or "")
    if agent.runtime.log_dir:
        _write_log(agent.runtime.log_dir, log)
    return RunResult(text=state.final_text or "", state=state, stop_reason=stop_reason, log=log)


async def _execute_calls(
    agent: Agent,
    ctx: ToolContext,
    state: RunState,
    calls: list[ToolCall],
    log: RunLog,
    debug,
) -> list[Observation]:
    by_name = {tool.name: tool for tool in agent.tools}
    any_serial = any(call.name in by_name and by_name[call.name].serial for call in calls)
    batch_seen: set = set()
    recorded: list[Observation] = []
    if any_serial:
        for call in calls:
            result = await _run_one(agent, ctx, state, call, log, by_name, debug, batch_seen)
            recorded.append(_record_result(agent, ctx, state, call, result))
            await _tool_result_hook(agent, ctx, call, result, log)
    else:
        results = await asyncio.gather(
            *(_run_one(agent, ctx, state, call, log, by_name, debug, batch_seen) for call in calls)
        )
        for call, result in zip(calls, results):
            recorded.append(_record_result(agent, ctx, state, call, result))
            await _tool_result_hook(agent, ctx, call, result, log)
    return recorded


async def _tool_result_hook(
    agent: Agent,
    ctx: ToolContext,
    call: ToolCall,
    result: ToolResult,
    log: RunLog,
) -> None:
    try:
        await agent.on_tool_result(ctx, call, result)
    except Exception as exc:
        log.record("hook_error", hook="on_tool_result", error=f"{type(exc).__name__}: {exc}")


async def _run_one(
    agent: Agent,
    ctx: ToolContext,
    state: RunState,
    call: ToolCall,
    log: RunLog,
    by_name: dict[str, Tool],
    debug,
    batch_seen: set,
) -> ToolResult:
    await debug(1, "tool_call_start", name=call.name, arguments=call.arguments)
    key = _call_key(call)
    if key in batch_seen or key in state.call_history:
        tool = by_name.get(call.name)
        cached = state.result_cache.get(key)
        if cached is not None and tool is not None and tool.cacheable:
            reused = ToolResult(
                ok=cached.ok,
                summary=f"Reused previous result: {cached.summary}",
                data=cached.data,
                error=cached.error,
                artifact_id=cached.artifact_id,
                truncated=cached.truncated,
                timed_out=cached.timed_out,
                blocked=False,
                cached=True,
                preview=cached.preview,
            )
            await debug(
                1,
                "tool_call_end",
                name=call.name,
                ok=True,
                cached=True,
                duration_ms=0,
                artifact_id=reused.artifact_id,
                truncated=reused.truncated,
            )
            log.record(
                "tool_call",
                step=state.steps,
                tool=call.name,
                arguments=call.arguments,
                duration_ms=0,
                ok=True,
                cached=True,
                artifact_id=reused.artifact_id,
                truncated=reused.truncated,
            )
            return reused
        first_step = state.call_history.get(key)
        blocked = ToolResult(
            ok=False,
            blocked=True,
            summary="Harness blocked duplicate tool call",
            error=(
                f"Tool {call.name} was already called with identical arguments"
                + (f" at step {first_step}" if first_step is not None else " in this batch")
                + "; change the arguments, use a different tool, or continue "
                "from any continuation hint the tool returned (e.g. a next offset or chunk)."
            ),
        )
        await debug(1, "tool_call_end", name=call.name, ok=False, error=blocked.error, duration_ms=0)
        log.record(
            "tool_call",
            step=state.steps,
            tool=call.name,
            arguments=call.arguments,
            duration_ms=0,
            ok=False,
            blocked=True,
            error=blocked.error,
        )
        return blocked
    batch_seen.add(key)
    tool = by_name.get(call.name)
    start = time.perf_counter()
    if tool is None:
        result = ToolResult(
            ok=False,
            summary="Unknown tool",
            error=f"Unknown tool {call.name!r}. Available: {', '.join(sorted(by_name))}",
        )
    else:
        result = await execute_tool(
            tool,
            ctx,
            call,
            agent.runtime.tool_timeout,
            agent.context.max_tool_result_chars,
        )
    await debug(
        1,
        "tool_call_end",
        name=call.name,
        ok=result.ok,
        error=result.error,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
        truncated=result.truncated,
        artifact_id=result.artifact_id,
    )
    log.record(
        "tool_call",
        step=state.steps,
        tool=call.name,
        arguments=call.arguments,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
        ok=result.ok,
        error=result.error,
        timed_out=result.timed_out,
        artifact_id=result.artifact_id,
        truncated=result.truncated,
    )
    return result


def _record_result(
    agent: Agent,
    ctx: ToolContext,
    state: RunState,
    call: ToolCall,
    result: ToolResult,
) -> Observation:
    state.tool_calls += 1
    if result.blocked:
        status = "blocked"
    elif result.cached:
        status = "cached"
    elif result.ok:
        status = "success"
        state.consecutive_failures = 0
    else:
        state.failures += 1
        state.consecutive_failures += 1
        status = "timeout" if result.timed_out else "error"

    key = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str))
    if key in state.call_history:
        state.repeated_calls.append(call.name)
        state.harness_notices.append(
            f"Tool {call.name} was called with identical arguments at step {state.call_history[key]}; "
            "repeating an identical call usually adds no new information. "
            "Change the arguments or try a different tool."
        )
    else:
        state.call_history[key] = state.steps

    if result.ok and not result.cached and not result.blocked:
        tool = next((t for t in agent.tools if t.name == call.name), None)
        if tool is not None and tool.cacheable:
            state.result_cache[key] = result

    preview = result.preview
    if preview is None and result.data is not None:
        preview = serialize_output(result.data)[: agent.context.max_tool_result_chars]
    observation = Observation(
        tool=call.name,
        status=status,
        summary=result.summary,
        preview=preview or "",
        arguments=call.arguments,
        truncated=result.truncated,
        artifact_id=result.artifact_id,
        step=state.steps,
    )
    state.observations.append(observation)
    if status == "success" and len(state.evidence) < agent.context.max_evidence_items:
        preview_text = (preview or "")[: agent.context.max_chars_per_evidence]
        key = (call.name, result.summary, preview_text)
        if key not in state._evidence_keys:
            state._evidence_keys.add(key)
            state.evidence.append(
                Evidence(
                    index=len(state.evidence),
                    tool=call.name,
                    summary=result.summary,
                    preview=preview_text,
                    source=str(
                        call.arguments.get("path")
                        or call.arguments.get("root")
                        or call.arguments.get("artifact_id")
                        or ""
                    ),
                )
            )
    if result.artifact_id:
        state.artifacts.append(
            Artifact(
                id=result.artifact_id,
                tool=call.name,
                summary=result.summary,
                size=ctx.artifact_store.size(result.artifact_id),
            )
        )
    return observation


def _guard_message(state: RunState, reason: str) -> str:
    lines = [
        f"The harness stopped this run before the model produced a final answer (reason: {reason}).",
        f"Run summary: {state.steps} model step(s), {state.tool_calls} tool call(s), {state.failures} failure(s).",
    ]
    if state.harness_notices:
        lines.append(f"Last harness notice: {state.harness_notices[-1]}")
    if state.observations:
        last = state.observations[-1]
        lines.append(f"Last observation: [{last.tool}] {last.status}: {last.summary}")
    return "\n".join(lines)


def _call_key(call: ToolCall) -> tuple[str, str]:
    return (
        call.name,
        json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str),
    )


async def _final_generation(
    agent: Agent,
    state: RunState,
    request: str,
    history: Optional[list[tuple[str, str]]],
    on_token,
    log: RunLog,
    debug,
) -> str:
    """One tool-free model call over a compact evidence summary.

    Grounded on the evidence collected this run (numbered, but citations are
    advisory rather than a mandatory contract — the harness stays general, not
    a knowledge-base assistant). Returns "" if it fails, so callers fall back
    to the existing answer/guard message.
    """
    now_line = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sections = [f"CURRENT TIME\n{now_line}", f"CURRENT USER REQUEST\n{request}"]
    if history:
        builder = ContextBuilder(agent.system_prompt, agent.context)
        for header, body in builder.history_sections(history):
            sections.append(f"{header}\n{body}")
    if state.facts:
        sections.append(
            "VERIFIED FACTS\n"
            + "\n".join(f"[{fact.id}] {fact.value}" for fact in state.facts)
        )
    if state.evidence:
        lines = []
        for item in state.evidence:
            lines.append(f"[{item.index}] ({item.tool}) {item.summary}")
            if item.preview and item.preview != item.summary:
                lines.append("    " + item.preview)
            if item.source:
                lines.append(f"    source: {item.source}")
        sections.append("AUTHORIZED EVIDENCE\n" + "\n".join(lines))
    body = (
        "\n\n".join(sections)
        + "\n\nAnswer the user's request directly based only on the information above. "
        "Do not call tools."
        + "\nIf you used tools to read files or documents, ground your answer in that "
        "evidence and do not invent facts or sources. For general or conversational "
        "questions, answer from your own knowledge — do not refuse just because no file "
        "matched. If the user asked about specific files and the evidence does not "
        "contain the answer, say what is missing or that you cannot confirm."
    )
    messages = [
        Message(role="system", content=agent.system_prompt),
        Message(role="user", content=body),
    ]
    final_started = time.perf_counter()
    await debug(
        1,
        "model_call_start",
        step=state.steps + 1,
        max_steps=state.steps + 1,
        phase="final",
    )
    try:
        response = await asyncio.wait_for(
            agent.model.respond(messages, [], on_token=on_token),
            timeout=agent.runtime.model_timeout,
        )
    except Exception as exc:
        log.record("final_generation_error", error=f"{type(exc).__name__}: {exc}")
        return ""
    await debug(
        1,
        "model_response",
        step=state.steps + 1,
        usage=response.usage,
        tool_calls=[],
        metrics=response.metrics,
        duration_ms=round((time.perf_counter() - final_started) * 1000, 1),
    )
    return response.text or ""


async def _reflect_experience(
    agent: Agent,
    state: RunState,
    request: str,
    stop_reason: str,
    log: RunLog,
    debug,
) -> Optional[dict]:
    """Distill a finished run into one reusable experience (JSON record).

    One tool-free model call: the model writes problem_type / keywords / method
    / result / success. ``method`` must stay methodology-only (which tools,
    what sequence, what to avoid) — never specific results. Returns None on any
    failure so the loop never depends on this pass.
    """
    if state.tool_calls == 0:
        return None
    try:
        tool_lines = []
        for obs in state.observations[-15:]:
            args = obs.arguments or {}
            compact = dict(list(args.items())[:3])
            rendered = json.dumps(compact, ensure_ascii=False, default=str)[:150]
            tool_lines.append(f"- step {obs.step}: {obs.tool}({rendered}) -> {obs.status}")
        now_line = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        body = (
            f"CURRENT TIME\n{now_line}"
            f"\n\nCURRENT REQUEST\n{(request or '')[:500]}"
            f"\n\nTOOL SEQUENCE\n{chr(10).join(tool_lines) or '(none)'}"
            f"\n\nFINAL ANSWER\n{(state.final_text or '')[:600]}"
            f"\n\nSTOP REASON: {stop_reason}"
        )
        prompt = (
            "You are the memory module of an agent harness. Distill this run into "
            "ONE reusable experience so the same task is faster next time. Record "
            "the METHODOLOGY (which tools, what sequence, what to avoid) — never "
            "specific results or snippets. time_sensitive is true only when the "
            "method's usefulness depends on WHEN it runs (weather, news, prices, "
            "stock, events); false for stable methodology (reading files, coding "
            "steps).\n\n"
            "Output ONLY a JSON object (no markdown fences, no commentary) with keys:\n"
            '{"problem_type": "<short category, e.g. weather_query|file_question|'
            'code_change|web_search>", "keywords": ["<3-8 searchable terms, in the '
            'same language as the request>"], "method": "<the simplest reusable path, '
            '<=200 chars>", "result": "<one line about the outcome>", '
            '"success": true_or_false, "time_sensitive": true_or_false}'
        )
        messages = [
            Message(role="system", content=prompt),
            Message(role="user", content=body),
        ]
        await debug(
            1,
            "model_call_start",
            step=state.steps + 1,
            max_steps=state.steps + 1,
            phase="reflect",
        )
        started = time.perf_counter()
        response = await asyncio.wait_for(
            agent.model.respond(messages, [], on_token=None),
            timeout=agent.runtime.model_timeout,
        )
        await debug(
            1,
            "model_response",
            step=state.steps + 1,
            usage=response.usage,
            tool_calls=[],
            metrics=response.metrics,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return _normalize_experience(
            _parse_json_object(response.text or ""), request, stop_reason, state
        )
    except Exception as exc:
        log.record("experience_reflect_error", error=f"{type(exc).__name__}: {exc}")
        return None


def _parse_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object from a model reply; return None if absent."""
    text = (text or "").strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_experience(
    data: Optional[dict],
    request: str,
    stop_reason: str,
    state: RunState,
) -> Optional[dict]:
    """Validate and normalize a reflected experience into a store-ready dict."""
    if not isinstance(data, dict):
        return None
    problem = str(data.get("problem_type") or "").strip()[:80]
    method = " ".join(str(data.get("method") or "").split())[:400]
    if not problem or not method:
        return None
    keywords = [str(k).strip()[:30] for k in (data.get("keywords") or [])]
    keywords = [k for k in keywords if k][:12]
    result = " ".join(str(data.get("result") or "").split())[:200]
    success = bool(data.get("success", True))
    time_sensitive = bool(data.get("time_sensitive", False))
    return {
        "request": (request or "").strip()[:200],
        "problem_type": problem,
        "keywords": keywords,
        "method": method,
        "result": result,
        "success": success,
        "time_sensitive": time_sensitive,
        "stop_reason": stop_reason,
        "steps": int(state.steps),
        "tool_calls": int(state.tool_calls),
    }


def _write_log(log_dir: str, log: RunLog) -> None:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    with (path / f"{log.run_id}.jsonl").open("w", encoding="utf-8") as handle:
        for entry in log.entries:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
