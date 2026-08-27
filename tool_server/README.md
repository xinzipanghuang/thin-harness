# Explicit remote tool server

This package is the remote execution boundary for trusted tools. It deliberately
does not import `tools/`, scan packages, accept a module name from a request, or
offer generic shell/SSH/Python execution. Startup code constructs a
`ServerToolRegistry` and registers each `ServerTool` instance explicitly.

## Execution environment

`ExecutableSpec(id="samtools", name="samtools")` resolves only in the
environment of the Python process that started the server: the directory of
`sys.executable` and `sys.prefix/bin`. It does not use `shutil.which()` over an
arbitrary inherited `PATH`. A tool may instead declare an absolute reviewed
path with `ExecutableSpec(id="samtools", path="/opt/samtools/bin/samtools")`.
Both forms are resolved and checked as executable when the tool is registered.

The child receives a strict environment allowlist, so server credentials such
as `TOOL_SERVER_TOKEN` and API keys are not inherited. `PATH` is rebuilt with
the server environment first, while `HOME` and `TMPDIR` point to the private
request working directory. Processes use an argument vector, `stdin=DEVNULL`,
and a new process group. No shell is involved.

## MCP interface

The sole public tool interface is MCP Streamable HTTP at `POST /mcp`. Tool
discovery uses the standard MCP `tools/list` method and execution uses
`tools/call`; there is no parallel custom REST API. `GET /healthz` is an
unauthenticated liveness endpoint only and exposes no registry or execution
data. Individual MCP requests are stateless and return bounded JSON responses;
durable long-skill state lives behind the transport in SQLite. This keeps the
wire interface straightforward for Codex, desktop clients, and
service-to-service callers using an MCP SDK.

`/mcp` requires a bearer token of at least 32 bytes. The transport binds to
`127.0.0.1` by default, enables DNS-rebinding protection there, and refuses a
non-loopback plaintext binding unless explicitly overridden. Production
exposure should keep the process on loopback behind a TLS reverse proxy or use
a private service mesh with its own workload identity policy. The fixed-token
gate is intentionally small; deployments that need user identity and delegated
authorization should replace it with standards-based OAuth at the edge.

MCP request bodies, complete JSON responses, the advertised tool catalog,
process arguments, concurrency, runtime, log files, and returned stdout/stderr
all have independent hard limits. Oversized tool results are not sliced into
invalid JSON: `tools/call` returns an MCP error result, and the tool must persist
large data in the approved data plane and return a compact URI or artifact
identifier instead.

Per-request working directories are deleted after each call by default. A
deployment may opt into `retain_run_files=True` for local audit, but then it
must enforce an external retention and disk-quota policy.

The MCP transport itself is stateless, while long-skill state, step checkpoints,
results, and bounded logs are durable in `server_runs/tasks.sqlite3`. Run only
one active server process per task database; multi-host leasing is not yet part
of this local executor.

Registered Python files are trusted code and remain a code-review boundary;
the registry cannot sandbox a malicious `ServerTool.run()` implementation.
For hostile workloads or stronger filesystem/resource isolation, place this
server or its runner behind containers, cgroups, a batch scheduler, or an HPC
execution account. Tool schemas should prefer dataset IDs/URIs over arbitrary
filesystem paths.

## Start the server

The package has a formal entry point. Run it from the repository root with a
strong token:

```bash
export TOOL_SERVER_TOKEN="$(openssl rand -hex 32)"
python -m tool_server
```

Point an MCP client at `http://127.0.0.1:8765/mcp` with an
`Authorization: Bearer ...` header. Liveness is available without
authentication at `GET http://127.0.0.1:8765/healthz`.

The production entry point registers `runtime.python.version`, the executable
smoke-test skill `runtime.environment.audit`, and the shared task controls by
default.
The following deployment settings are supported:

| Environment variable | CLI option | Default |
| --- | --- | --- |
| `TOOL_SERVER_HOST` | `--host` | `127.0.0.1` |
| `TOOL_SERVER_PORT` | `--port` | `8765` |
| `TOOL_SERVER_WORK_ROOT` | `--work-root` | `server_runs` |
| `TOOL_SERVER_ENABLE_SAMTOOLS=1` | `--enable-samtools` | disabled |
| `TOOL_SERVER_RETAIN_RUN_FILES=1` | `--retain-run-files` | disabled |
| `TOOL_SERVER_ALLOW_INSECURE_REMOTE=1` | `--allow-insecure-remote` | disabled |

Enabling samtools registers `bio.samtools.version` and fails closed during
startup if `samtools` is not installed in the Python server environment.
Binding plaintext HTTP outside loopback also requires the explicit insecure
override; normal remote deployment should put the loopback server behind a TLS
reverse proxy.

See `examples/bio_tool_server.py` for a smaller explicit-assembly example.

## Durable long skills

Long skills are reviewed Python contracts, not files accepted from a request.
They sit behind a registry and fixed workflow executor:

```text
MCP tools
  |- atomic tools
  |- skill.<id>.submit
  `- task.get / task.result / task.cancel / task.logs
          |
LongSkillRegistry + DurableTaskStore (SQLite)
          |
fixed ToolStep executor
          |
registered ServerTool / executable environment / external scheduler adapter
```

Only `LongSkill(status="approved")` instances explicitly registered in startup
code receive a submit Tool. Each definition has closed Pydantic input/output
models, fixed ordered steps and dependencies, a total deadline, a per-skill
concurrency limit, fixed retry counts, output bindings, and a stable contract
hash. Registration fails if a step names an unavailable atomic Tool or if its
graph or bindings are invalid.

The SQLite store persists input, status, contract hash, step attempts and
outputs, bounded result/error objects, and bounded paged logs. On restart,
unfinished tasks are queued again and completed steps are skipped. A changed
Skill contract or resolved atomic Tool version/Schema changes the hash and fails
closed instead of resuming under different code.

Cancellation is immediate for queued work. For running work it sets durable
cancellation intent and signals the active executor. Subprocess-based atomic
Tools terminate their process group; trusted pure-Python Tools observe the
cancellation at the next workflow checkpoint. Scheduler-backed Tools should
map that same signal to their scheduler's cancel operation.

Results larger than the configured JSON limit are rejected with instructions
to persist the data and return an artifact URI. Logs are independently bounded
and paginated. The built-in smoke-test definition is in
`tool_server/builtin_skills.py`; production bioinformatics skills should be
added as separate reviewed definitions after their atomic Tool contracts and
artifact data plane are fixed.

The public compatibility interface currently remains ordinary MCP Tools:

```text
skill.runtime.environment.audit.submit
task.get
task.result
task.cancel
task.logs
```

MCP 2025-11-25 native Tasks remain an adapter-layer concern. When client support
is mature, native `tasks/get`, `tasks/result`, and `tasks/cancel` handlers can be
added without changing `LongSkillRegistry`, `DurableTaskStore`, or the executor.
