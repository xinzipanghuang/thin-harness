# Minimal Codex-Style Agent Runtime

**English** · [简体中文](./README.zh-CN.md)

*Also known as: `thin-harness`* — from the design principle "thin harness"

thin-harness is a minimal local runtime for understanding, experimenting with,
and building controllable domain agents: **you define the domain, tools, and
policy; the harness runs the loop.**

- **Your agents.** Subclass `Agent`; pick the prompt, tools, and limits —
  behavior stays under your control. No YAML.
- **Your tools.** A typed Python function with `@tool`; auto-discovered.
- **Your model.** Four values in `.env`, any OpenAI-compatible endpoint.

A document FAQ, a bioinformatics assistant, a coding helper, or a daily
assistant all run on the same stable core. Adding a domain should add a domain
package, not branches to the main loop. No YAML, hidden planners, critics, or
multi-agent orchestration.

## Who this is for

- **Developers who want to read and own their agent loop** — the whole
  runtime is a handful of files; change whatever you need.
- **Anyone who wants a local, terminal-based general assistant** that chats
  normally and can also inspect files, read documents, run commands, or write
  code — with one `.env` pointing at any OpenAI-compatible endpoint.
- **People building a small domain agent** (document FAQ, a coding helper, a
  daily assistant): define it as a Python subclass, add tools as decorated
  functions, and you are done.
- **Learners** — a compact, readable reference for how a Codex-style agent
  loop, tool calling, and memory can be built.

Not aimed at teams that need a managed RAG/knowledge-base product,
large-scale multi-agent orchestration, or a full plugin ecosystem.

## References

- [OpenAI Codex](https://github.com/openai/codex) — the agent loop design:
  model -> tools -> observations, append-only context, deterministic guards.
- [nanobot](https://github.com/chrishayuk/nanobot) — the terminal chat shape:
  rich rendering plus a simple async message bus.

Both are design references only; no code is copied or imported.

## Extensible by design

| To extend...      | How                                                     | Example                                  |
| ----------------- | ------------------------------------------------------- | ---------------------------------------- |
| a new tool        | a typed function + `@tool` in `tools/`                  | `@tool def read_file(path, max_chars=...)` |
| a domain agent    | subclass `Agent`, declare prompt / tools / limits       | `class MyAgent(Agent)`                    |
| a new model       | edit `.env` (`LLM_BASE_URL` + `LLM_MODEL`)              | DeepSeek -> DashScope -> LiteLLM          |
| runtime behavior  | override hooks (`bootstrap`, `on_tool_result`)          | FAQ document pre-search                   |

## Core design

- **One loop.** `Model -> Tool calls -> Environment -> observations -> Model`,
  with deterministic guards only (step/tool budgets, timeouts, no-gain
  cutoff, duplicate-call handling). No hidden reasoning or reflection calls.
- **Append-only context.** Message history is append-only, so the prompt
  prefix stays byte-stable for provider prefix caching. Recent conversation
  turns are kept verbatim so references like "that document" resolve; older
  turns are clipped.
- **Structured world state.** Observations, facts, and artifacts live in a
  `RunState`, not in an ever-growing raw message list.
- **Grounded guard recovery.** A normally completed answer is returned
  directly. If a deterministic guard stops the loop first, one tool-free call
  synthesizes an answer over numbered evidence (`AUTHORIZED EVIDENCE`).
  Citations remain advisory rather than a mandatory knowledge-base contract.
- **Read-only cache reuse.** Exact repeated calls to read-only tools return
  the previous result (marked `Reused previous result`) instead of
  re-executing or erroring; non-cacheable duplicates are blocked.
- **Tools are typed Python functions.** Auto-discovered from `tools/`, schema
  generated from type hints and docstrings, no central registration file.
- **Agents are Python subclasses.** No YAML. An agent declares prompt, tool
  selection, and limits; the model always comes from `.env`.

### The loop in sequence

```mermaid
sequenceDiagram
    participant L as Agent loop
    participant M as Model
    participant T as Tools
    L->>M: respond(messages, tools)
    alt model returns tool calls
        M-->>L: function calls
        L->>T: execute (parallel / serial)
        T-->>L: observations
        L->>L: append messages, update RunState, apply guards
    else model returns final text
        M-->>L: answer
    end
    opt a guard stops the loop before an answer
        L->>M: final synthesis (tool-free, over numbered evidence)
        M-->>L: grounded answer
    end
```

## Architecture

```text
core/
  agent.py     Agent = Model + System Prompt + Selected Tools + Harness Policy
  loop.py      Core loop + deterministic guards; cache reuse; final pass
  model.py     Model interface + OpenAIModel (OpenAI SDK, any compatible
               base_url) + ScriptedModel/EchoModel (offline)
  providers.py Generic .env (key/base_url/model/thinking) -> OpenAI SDK
  registry.py  Agent registration and declared domain-package discovery
  context.py   Append-only ContextBuilder: base messages + incremental appends
  tool.py      Recursive declared-package discovery + execution/normalization
  types.py     RunState, provenance, trace export, evidence, artifacts...
  artifacts.py Artifact store for large tool outputs
  storage.py   SQLite memory via peewee (Session/Turn/Fact/Artifact/DebugEvent)

tools/         Auto-discovered shared tools (each module = a namespace)
  filesystem.py  read (windowed: offset/next_offset), write, list, find, grep
  shell.py / python.py   subprocess execution
  artifacts.py  artifacts.read
  documents.py  documents.list/read/search/progress (pdf/docx/txt/...)
  daily.py      daily.now (local time), daily.todo (local todo list)

agents/        Built-in agents, registry, and compatibility imports
  daily_agent.py  DailyAgent (default): full tool set + daily.* helpers
  coding_agent.py CodingAgent: filesystem/shell/python focus
  faq_agent.py    compatibility import for domains.faq
domains/       Cohesive domain packages: Agent + prompt + owned tools
  faq/         document-grounded FAQ
  bioinformatics/ FASTA/FASTQ/VCF inspection + structured command execution
prompts/agent.md  Generic agent instructions (shared; "decide first, then act")
chat/          Terminal chat: rich channel + simple async message bus
  bus.py / worker.py / channel.py
examples/      offline loop, FAQ, and bioinformatics entry points
```

### Runtime flow

```mermaid
flowchart LR
    U[User] -->|message| C[TerminalChannel<br/>rich UI]
    C -->|inbound| B[(MessageBus)]
    B --> W[AgentWorker]
    W --> L[Agent loop<br/>core/loop.py]
    L -->|respond| M[Model<br/>OpenAI SDK, any endpoint]
    M -->|tool calls| L
    L -->|execute| T[Tools<br/>filesystem / documents / shell / python / daily]
    T -->|observations| L
    L -->|reply| B
    B -->|outbound| C
    C -->|answer| U
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in LLM_API_KEY / LLM_BASE_URL
python -m chat          # default: daily agent, terminal chat
```

As a library:

```python
from agents import create_agent

agent = create_agent()  # DailyAgent by default
result = await agent.run("Check the git status of this project.")
print(result.text)
print(result.trace_json())  # complete observable trajectory
```

Offline demo (no API key):

```bash
python -m chat --demo
python examples/mock_run.py
```

Domain examples:

```bash
python examples/faq_run.py "What is the refund policy?" --documents ./docs
python examples/bioinformatics_run.py ./data/sample.vcf
```

## Agents

An agent is a Python subclass of `core.agent.Agent` — not a YAML file. It only
defines the domain: `prompt` / `prompt_path`, `tool_include` /
`tool_exclude`, `own_tools`, and runtime limits. The model is constructed
from `.env` by the provider layer, so an agent never configures a model:

```python
from core.agent import Agent
from core.tool import agent_tool


@agent_tool
def hello(name: str) -> str:
    """Say hello to someone."""
    return f"hello {name}"


class MyAgent(Agent):
    name = "my-agent"
    prompt = "You are a friendly assistant."
    own_tools = [hello]   # private to this agent only


agent = MyAgent()
result = await agent.run("say hello to codex")
print(result.text)
```

`@agent_tool` makes a tool private to one agent; shared tools go in `tools/`
with `@tool` (see below).

Tools owned by a reusable domain live under that domain's `tools/` package.
Return `ToolResult(..., provenance={...})` when source files, command lines,
versions, parameters, or generated outputs are material to reproducibility.

Built-in agents (pick with `--agent`, default `daily`):

- `daily` — full shared tool set (`filesystem.*`, `shell.run`, `python.run`,
  `documents.*`, `artifacts.read`, `daily.now`, `daily.todo`), for everyday
  local work.
- `coding` — filesystem/shell/python focus for code tasks.
- `faq` — answers questions from documents: finds them with `documents.list`,
  reads them in character windows (default 200, max 6000) via
  `documents.read` (offset/next_offset continuation), locates keywords with
  `documents.search`, tracks coverage with `documents.progress`, and cites
  the source. Supported formats: PDF, DOCX, and common text files.
- `bioinformatics` — inspects FASTA, FASTQ, and VCF files with typed tools,
  can run local bioinformatics executables without a shell, and preserves
  source/command/version provenance in the final output and run trace.

Domain agents compose the shared prompt with a domain prompt, declare their
own tool packages, and register themselves without editing the core loop:

```python
from core.agent import Agent
from core.registry import register_agent


@register_agent("my-domain")
class MyDomainAgent(Agent):
    prompt_paths = ["prompts/agent.md", "domains/my_domain/prompt.md"]
    tool_packages = ["tools", "domains.my_domain.tools"]
    tool_include = ["filesystem.read", "my_domain.*"]
```

### Agent hierarchy

```mermaid
classDiagram
    class Agent {
        +name
        +prompt_path
        +tool_include
        +max_steps
        +run()
        +bootstrap()
        +on_tool_result()
        +finalize()
    }
    class DailyAgent
    class CodingAgent
    class FAQAgent
    class BioinformaticsAgent
    Agent <|-- DailyAgent : full tools + daily.*
    Agent <|-- CodingAgent : filesystem / shell / python
    Agent <|-- FAQAgent : documents Q&A
    Agent <|-- BioinformaticsAgent : typed bioinformatics tools
```

## Model config (.env — no vendor catalog)

`.env` holds exactly four values:

```env
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_ENABLE_THINKING=false
```

The provider layer resolves them and talks to any OpenAI-compatible endpoint
(DeepSeek, Alibaba DashScope, LiteLLM proxy, ...) through the OpenAI SDK.
Resolution order per field: explicit config -> env var -> default. Switching
models is editing `.env`, nothing else.

Note: `LLM_ENABLE_THINKING` is passed through for endpoints that support it;
on DeepSeek, thinking is controlled by the model choice (`deepseek-chat` vs a
reasoner variant).

## Terminal chat

```bash
python -m chat                      # daily agent
python -m chat --agent faq          # document Q&A
python -m chat --agent bioinformatics  # typed bioinformatics tools
python -m chat --demo               # offline echo
python -m chat --progress           # per-step progress lines + live timer
python -m chat --debug 1|2|3        # leveled runtime debug
python -m chat --session work       # memory session (persists across restarts)
python -m chat --db data/other.db   # custom SQLite path
python -m chat --markdown           # full live Markdown (ANSI terminal)
```

In-chat commands: `/help`, `/clear` (wipe session memory), `/tools` (list the
current agent's tools with purposes), `/trace` (show the complete last run),
`/trace RUN_ID` (load a persisted trace), `/exit`. `Ctrl+C` quits cleanly;
`Shift+Enter` inserts a newline so a message can span several lines.

Answers stream token by token and are rendered as Markdown line by line (no
redraws, works on any terminal). By default the UI stays quiet: only
`agent is working…` and the final `[t=…] N steps · M tool calls` summary
appear. With `--progress`, each step prints with a running `[t=…]` timestamp
and a live in-place timer during model calls. Debug events flow through the
same bus and never pollute the model context.

## Memory & persistence (SQLite + peewee)

Codex-style memory in `core/storage.py` (default `data/agent.db`):

- `Session` — one row per chat session;
- `Turn` — every run (request, response, stop reason, counters);
- `Fact` — verified facts, session-scoped or global (`Memory.remember`);
- `Artifact` — full tool outputs, survive the run;
- `Experience` — "evolution" memory: one JSON document per reusable
  methodology (`problem_type`, `keywords`, `method`, `result`, `success`),
  upserted per normalized request or same problem type with ≥2 shared
  keywords, ranked by usage, deduplicated on startup, and time-aware:
  each record carries `learned_at` / `last_used_at` / `time_sensitive`, and
  time-sensitive records older than `experience_stale_days` (default 7) are
  not injected until re-verified;
- `DebugEvent` — structured per-run debug records, persisted regardless of
  UI debug level (inspect with `Memory.load_debug(run_id)`).

The experience module is per-agent configurable: set `experience_enabled =
False` on an `Agent` subclass (e.g. `FAQAgent` does this) to disable both
injection into context and recording after runs.

Experiences update in place: re-asking the same task (even reworded) merges
into the existing record instead of creating a duplicate; `daily.update`
edits a record directly from chat (fix a method, replace keywords, or mark it
failed) and `daily.forget` deletes one.

Time is part of every run: the context header always includes `CURRENT TIME`
(local clock + timezone), and the reflection pass records whether a method is
time-sensitive — so the agent knows "today" without calling a tool and stops
trusting stale weather/news methods until they are re-verified.

The agent itself carries time state: each `Agent` instance exposes
`started_at`, `last_run_at` and `now()` for hooks/tools, and when a session
already exists the context header also shows `SESSION STATE` (when the
conversation started and its last activity, with human-readable "X ago") — so
resuming a multi-day conversation never confuses the agent about its timeline.

Environment awareness: the context header also includes an `ENVIRONMENT`
section (OS + release/arch, Python version, detected terminal, shell, user,
and cwd), mirrored on the agent as `agent.environment` and in `ToolContext`,
so the model and tools can adapt to the platform without probing.

The loop loads prior turns and facts into context at run start — the most
recent `history_recent_turns` (default 3) verbatim, earlier turns compressed
to a one-line summary (`CONVERSATION SUMMARY`) — injects matched experiences
as `RELEVANT EXPERIENCE`, and saves the run at the end. After a completed run
that used tools, the model distills the run into one JSON experience
(`daily.forget` deletes incorrect records; `daily.experiences` lists them).
Restart `python -m chat` with the same `--session` and it remembers the
conversation.

Recency is explicit: history turns are labeled with their global session
number, the recent block ends by marking the latest turn, and `CURRENT USER
REQUEST` is always the last section; `VERIFIED FACTS` is capped at
`max_facts` (default 15) so stale topics cannot crowd out the recent thread.

Debug records stay trustworthy: `Turn.seq` is migrated to `AUTOINCREMENT` (so
cleared sessions never recycle turn numbers), and on startup `Memory` prunes
debug events that reference deleted turns or impossible old timestamps — use
`Memory.load_debug(run_id)` for per-run audit data.

## Adding a tool

Tools are the primary extension surface. The runtime discovers them
automatically and generates their schemas from type hints, defaults, and
docstrings — there is no central registration file to edit.

Create a file under `tools/` and decorate a typed function:

```python
from core.tool import tool


@tool
def read_file(path: str, max_chars: int = 1000) -> str:
    """Read a text file."""
    ...
```

The tool name is `<module>.<function>`, the schema is generated from type
hints/defaults/docstrings, and no central registration file needs editing.
Registry names keep dot namespaces (`filesystem.read`); the loop maps them to
API-safe names (`filesystem_read`) for the model and back before executing.
Tools may declare `ctx: ToolContext` to access the artifact store, workdir,
request, or `ctx.record_fact(...)`. `@tool(serial=True)` forces a tool to run
alone; `@tool(cacheable=True)` lets exact repeats reuse the previous result.
`@agent_tool` defines tools private to one agent (`own_tools`).

Agents can also override behavior hooks: `on_run_start`, `bootstrap` (cheap
pre-model work, e.g. the FAQ pre-search), and `on_tool_result`.

## Loop guards (deterministic only)

- `max_steps` / `max_tool_calls` — model and tool budgets;
- `tool_timeout` / `model_timeout` / `request_timeout` — time budgets;
- repeated calls — cacheable tools reuse the result (`cached` observation);
  others are `blocked` with a hint to change arguments or continue via
  `next_offset`;
- `max_consecutive_no_gain` — N rounds with no new evidence force the final
  pass;
- `max_consecutive_failures` — stop a run that keeps failing;
- `final_regenerate` — one tool-free pass over numbered evidence when a
  deterministic guard stops the loop before the model answers; normally
  completed answers are returned directly;
- `token_budget_tokens` (ContextConfig) — when set, exceeding the estimated
  budget (bytes/4) injects a stop hint; per-call timings include TTFT
  (`connect_ms` / `ttft_ms` in debug).

No scoring systems, reflection calls, planners, or intent classifiers.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The suite covers tools/schema, the loop (guards, cache reuse, evidence final
pass), context building, agents, memory, document window reading, and the
terminal chat (bus, worker, rendering, input commands).

## Security notes

- `shell.run` and `python.run` execute with the current user's privileges —
  local development tools, not sandboxes.
- Filesystem tools enforce read/write size limits and UTF-8 handling, but do
  not jail the agent to a directory.

## Deliberate omissions / extension points

- No skills, workflows, DAGs, planner/critic agents, multi-agent
  orchestration, semantic routing, or embedding-based tool selection.
- Artifacts are in-memory per run (disk persistence is a future extension).
- Fact extraction is explicit only (`ctx.record_fact`); no automatic critic.
- History compaction is deferred: recent turns are verbatim, older ones are
  clipped, and the token budget is estimated — full summarization compaction
  is the documented next step for very long sessions.

## License

MIT — see [LICENSE](LICENSE).
