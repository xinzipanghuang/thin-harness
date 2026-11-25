# WES support Agent

The `wes` domain connects the thin harness to the WES Center MCP Streamable HTTP
endpoint. The Agent never connects to Wuhan or Shenzhen directly.

Local test configuration:

```bash
export WES_MCP_URL=http://127.0.0.1:9090/mcp
python -m chat --agent wes --debug 1 --markdown
```

An unauthenticated connection is accepted by the client only for
`127.0.0.1`, `localhost`, or `::1`. For any remote center, configure a token of
at least 32 UTF-8 bytes:

```bash
export WES_MCP_URL=https://wes-center.example.internal/mcp
export WES_MCP_TOKEN='replace-with-a-strong-secret-at-least-32-bytes'
python -m chat --agent wes
```

Optional settings:

```text
WES_MCP_TOKEN_ENV=WES_MCP_TOKEN
WES_MCP_CONNECT_TIMEOUT=5
WES_MCP_CALL_TIMEOUT=120
WES_MAX_STEPS=64
WES_MAX_TOOL_CALLS=160
WES_REQUEST_TIMEOUT=1800
```

The execution budget defaults to 64 model steps, 160 tool calls and 1800 seconds
per request. Explicit constructor runtime settings take precedence over these
environment variables.

The Agent prefers consolidated `wes_*` tools. If none exist, it loads the
center's current compatibility catalog so the integration can be tested during
migration. Family lookup uses `wes_search_families` for Run ID, task ID, or
member-sample search and `wes_get_family` for complete declared roles and
regional index state. These roles are operational metadata, not kinship proof.
