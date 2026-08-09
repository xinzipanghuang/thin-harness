# Domain packages

A domain package owns the behavior that should vary between specialist
agents. The runtime in `core/` must not import a specific domain.

Recommended shape:

```text
domains/<name>/
  __init__.py          exports the Agent class
  agent.py             prompt composition, tool policy, budgets, hooks
  prompt.md            domain instructions
  tools/               typed, structured domain tools
```

The Agent registers itself with `@register_agent("name")` from
`core.registry`, declares shared
and domain tool packages through `tool_packages`, and selects the exposed
surface through `tool_include` / `tool_exclude`.

Domain tools should return `ToolResult` with compact structured `data` and a
`provenance` dictionary. Provenance should describe whatever makes the result
reproducible: source files, program and version, exact command, parameters,
reference identifiers, and output artifacts.

Use lifecycle hooks only for deterministic domain behavior:

- `bootstrap`: cheap preparation before the first model call;
- `on_tool_result`: record an explicit domain fact or update run-local state;
- `finalize`: validate or format the final output without another model call.

Adding a domain should not require a branch in `core/loop.py`.
