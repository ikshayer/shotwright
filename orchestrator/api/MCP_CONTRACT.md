# Contract — MCP adapter over the orchestrator API

One agent must see both this orchestrator's operations and FilmCraft's MCP tools. A client
runs several MCP servers side by side, so nothing is merged: the only missing piece is an
MCP server that speaks for this orchestrator. This contract pins it.

Read `CONTRACT.md` in this directory first. Everything there still holds; this file is
additional to it.

## Architecture

The adapter is a **pure client of a running orchestrator server**. It does not build a
FastAPI app, a `JobRunner`, an `OperationContext` or a store. Building an app in the
adapter process would start a second adapter job runner beside the server's, and the
server already owns the job queue and the GPU lease.

```
MCP client ──stdio JSON-RPC──▶ orchestrator.api.mcp ──loopback HTTP + token──▶ serve()
```

- **Tool discovery** is a `GET /tool-manifest` against the running server, so the tools an
  agent sees are exactly the routes the server it calls actually serves. No version skew is
  possible between the list and the server.
- **Tool calls** are one loopback HTTP request each, carrying `X-Loopback-Token`.

## Part A — server side (`orchestrator/api/manifest.py`, `orchestrator/api/app.py`)

### A1. The manifest publishes query parameters

`build_manifest` currently keeps only `"in": "path"` parameters. The four asset operations
(`search_assets`, `list_asset_categories`, `asset_preview`, `asset_browser`) take their
inputs as **query** parameters, so today they are published with no inputs but
`project_id` and are uncallable from the manifest. This is a defect; fix it.

- Query parameters become top-level properties of `input_schema`, exactly like path
  parameters, carrying their OpenAPI `schema` and listed in `required` when the OpenAPI
  parameter is required.
- Header parameters (`X-Loopback-Token`) are **never** published as inputs. The token is
  transport, not an argument an agent supplies.
- If a path parameter, a query parameter and `payload` would share a property name,
  `build_manifest` raises `ValueError` naming the route and the name. It never silently
  lets one shadow another.

### A2. Each tool says where its arguments go

Every tool's `http` object becomes:

```python
"http": {
    "method": "GET",                    # unchanged
    "path": "/projects/{project_id}",   # unchanged, still the template
    "path_params": ["project_id"],      # names substituted into the template, in order
    "query_params": [],                 # names sent as query string
    "body": None,                       # "json" | "multipart" | None
}
```

and every tool gains `"asynchronous": bool` — `True` exactly when the route's declared
success status is `202`. Derive it from the route (`route.status_code`), not by importing
`ASYNC_ADAPTER_OPERATIONS`; the route table is the single source.

Existing keys (`name`, `description`, `input_schema`, `http.method`, `http.path`, the
top-level `components`) keep their current meaning. Existing tests must still pass.

### A3. `GET /tool-manifest`

`create_app` registers one more route, `GET /tool-manifest`, returning
`build_manifest(app)` as JSON.

- It is a **transport meta-route**, like FastAPI's own `/openapi.json`, so it is **not** an
  entry in `OPERATIONS`, is registered with `include_in_schema=False`, and therefore is
  neither a tool, a CLI command nor part of the parity tests.
- It is built **per request** from `app.routes`. Never cache it on `app.state`; see
  `CONTRACT.md` "The manifest is keyed on the route name and never reads a cached schema".
- It is a read: it passes through the existing middleware (Host, Origin) and needs no
  token, like every other `GET`.

## Part B — the adapter (`orchestrator/api/mcp.py`)

A single module. Standard library plus `orchestrator.platform.settings` only — no new
dependency, no MCP SDK. It must **not** be imported by `orchestrator/api/__init__.py`.

### B1. Interface — binding on implementation and tests alike

```python
TOOL_PREFIX = "pipeline_"
TOKEN_ENV = "CCTV_PIPELINE_TOKEN"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first

@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str          # the Content-Type header, "" if absent
    body: bytes

class TransportError(Exception):
    """The server could not be reached or did not answer in time."""

class Transport(Protocol):
    def request(self, method: str, path: str, *,
                query: list[tuple[str, str]] | None = None,
                json_body: Any = None) -> HttpResponse: ...
    # `path` is already substituted and percent-encoded, e.g. "/projects/p%2F1".
    # `json_body=None` means send no body. Any status, 4xx/5xx included, is returned as an
    # HttpResponse; only failure to get a response at all raises TransportError.

class LoopbackTransport:            # implements Transport
    def __init__(self, base_url: str, token: str, *, timeout: float) -> None: ...

def tools_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]: ...   # pure

class McpServer:
    def __init__(self, transport: Transport) -> None: ...
    def handle(self, message: Any) -> dict[str, Any] | None: ...
    # One already-decoded JSON-RPC message in, the response object out, or None for a
    # notification. Never raises for any input.

def serve_stdio(server: McpServer, stdin: BinaryIO, stdout: BinaryIO) -> None: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

`python -m orchestrator.api.mcp [--settings settings.toml] [--timeout SECONDS]` runs
`main`. Add a console script `cctv-pipeline-mcp = "orchestrator.api.mcp:main"` to
`pyproject.toml`. `--timeout` defaults to 120.

### B2. Configuration and startup

- The base URL comes from `Settings.server.host` / `.port` (loaded with
  `load_settings(..., verify_paths=False)` when `--settings` is given, else `Settings()`;
  the adapter starts no Blender, FFmpeg or ComfyUI, so their paths are not its concern). Settings already refuses a non-loopback
  host; there is deliberately **no** `--url` override. An IPv6 host is bracketed in the URL.
- The token is read from the environment variable `CCTV_PIPELINE_TOKEN`. If it is absent
  or empty, `main` writes one line to stderr naming the variable and returns `2` without
  reading stdin. It never reads a token from argv (argv is visible to other processes).
- The token must be visible ASCII (`0x21`–`0x7E`) only. Anything else — a trailing CR/LF
  from a copied file is the likely case — makes `main` return `2` with a message that
  does not contain it. (`http.client` rejects such a header with a `ValueError` quoting
  the whole value, which would otherwise travel back to the client.) `LoopbackTransport`
  enforces the same rule with `ValueError` at construction.
- If any command-line argument contains the token, `main` returns `2` saying the token
  must not be passed on the command line, and echoes nothing.
- An invalid command line returns `2` (not `SystemExit`) and **does not echo the
  arguments**: argparse's own message quotes them, so a token passed as `--token X` by
  mistake would be copied into stderr, which MCP clients commonly log to disk.
- `main` does not contact the server at startup. The server may legitimately start after
  the client launches the adapter; reachability is reported per request (B5).

### B3. `LoopbackTransport` — the token must reach only the orchestrator

- **No proxies.** urllib honours `HTTP_PROXY` and, on Windows, the registry proxy; either
  would send the token to a third party. Build the opener with `ProxyHandler({})`.
- **No redirects.** urllib forwards custom headers across a redirect. A 3xx is returned
  to the caller as an `HttpResponse`, never followed.
- `X-Loopback-Token` is sent on every request. `Accept: application/json`. A JSON body is
  sent as UTF-8 with `Content-Type: application/json`. No `Origin` header is sent.
- Connection refused, DNS/socket failure or timeout raise `TransportError` whose message
  names the base URL and says the orchestrator server may not be running. The message,
  and every other string the adapter emits, **never contains the token**.

### B4. `tools_from_manifest` — manifest tool → MCP tool

For each manifest tool, in manifest order:

- `name` = `TOOL_PREFIX + tool["name"]`. FilmCraft's MCP server already exposes
  `render_preview` and `project_inspect`, so an unprefixed name would let an agent call
  the wrong server's tool.
- `description` = the manifest description; if `asynchronous`, append
  `" Asynchronous: answers 202 with a job record; poll pipeline_report_jobs for its state."`
- `inputSchema` = the manifest `input_schema` made **self-contained**: every
  `"$ref": "#/components/schemas/X"` is rewritten to `"#/$defs/X"`, and `$defs` holds the
  transitive closure of components that tool references (rewritten the same way) — not all
  components. Recursive references must terminate. A tool with no references has no
  `$defs`. A reference to a component that does not exist raises `ValueError`.
  **`discriminator.mapping` values are references too**: OpenAPI writes them as bare
  strings, not under a `$ref` key, so they are rewritten and counted toward the closure
  the same way. (Found by the implementer: `draft_script`, `revise_script`,
  `propose_shots` and `revise_shot` otherwise kept `#/components/` in their schema.)
- `annotations` = `{"readOnlyHint": True}` for `GET`, `{"readOnlyHint": False}` otherwise.
- **Upload operations (`http.body == "multipart"`) are excluded.** An MCP tool cannot carry
  file bytes sensibly, and having the adapter read arbitrary local paths for an agent is a
  surface this contract does not grant. Use the CLI's `--file` for uploads. This is an
  explicit, documented omission, not a silent one.
- The input manifest is not mutated.

### B5. `McpServer.handle` — JSON-RPC 2.0

| Message | Response |
|---|---|
| `initialize` | `protocolVersion`: the client's requested version if it is in `SUPPORTED_PROTOCOL_VERSIONS`, else `SUPPORTED_PROTOCOL_VERSIONS[0]`; `capabilities: {"tools": {"listChanged": false}}`; `serverInfo: {"name": "cctv-pipeline", "version": <package version>}` |
| any notification (no `id`) | `None` — including `notifications/initialized` and `notifications/cancelled`. An unexpected failure while handling one is written to stderr, never dropped. |
| `ping` | `result: {}` |
| `tools/list` | fetch `GET /tool-manifest` **fresh**, answer `{"tools": tools_from_manifest(...)}`, and keep that manifest for routing calls. No pagination. |
| `tools/call` | see below |
| any other method | error `-32601` |
| not an object / no `jsonrpc: "2.0"` / no string `method` | error `-32600`, `id` echoed when it is a string or integer, else `null` |

`tools/list` failures — `TransportError`, a non-200 status, or a body that is not a
manifest — are JSON-RPC error `-32603` whose message says why. A manifest is unusable,
and fails discovery rather than the later call, if any tool lacks a string `name`, or an
`http` block with string `method` and `path`, list `path_params` and `query_params`, and
`body`. Never an empty tool list:
an empty list would look like a server with no tools.

**`tools/call`.** If no manifest is held yet, fetch one first, as `tools/list` does.

- A name that is not a published tool (not prefixed, unknown, or an excluded upload
  operation) is JSON-RPC error `-32602`.
- `arguments` absent or `null` means `{}`; any other non-object is `-32602`. (`null` is
  accepted deliberately, for interoperability with clients that send it for no-argument
  tools.)
- **Argument routing**, from the tool's `http` block: each `path_params` name is
  substituted into the template, percent-encoded with `urllib.parse.quote(str(v), safe="")`.
  **Encoding alone does not isolate a segment**: the server percent-decodes the path
  before route matching, so `"p/assets/categories"` sent as `p%2Fassets%2Fcategories`
  reaches a *different operation* with the same verb, bypassing whatever approval the
  client gave for the named tool. So a path value that is empty, `"."`, `".."`, or
  contains `/` is a tool error naming the argument, and is never sent. (Found in review.) each `query_params` name present in
  the arguments is sent as a query pair — a list becomes repeated pairs, `bool` becomes
  `"true"`/`"false"`, `None` is omitted; when `body == "json"` and `payload` is present,
  it is sent as the JSON body.
- A missing required path parameter, or an argument the tool does not declare, is a
  **tool error** (below) naming the argument — not a protocol error — so the model can
  correct itself. The adapter does not otherwise validate against the schema; the server
  does, and its 422 is reported as a tool error.
- `TransportError` → tool error carrying the transport message.

**Tool results.** Every completed HTTP exchange is a `result` (never a JSON-RPC error),
with `isError` true exactly when the status is not 2xx:

- JSON response: one text content block whose text is
  `json.dumps({"http_status": status, "body": <decoded JSON>}, ensure_ascii=False)`.
  The status matters: `202` is how an agent learns a job was queued rather than done.
- `image/png|jpeg|webp` (`asset_preview`): one `{"type": "image", "data": <base64>,
  "mimeType": ...}` block.
- Any other content type (`asset_browser`'s HTML): one text block with the body decoded as
  UTF-8 (`errors="replace"`), preceded by nothing.
- A tool error that never reached the server (missing argument, transport failure):
  `isError: true` with one text block `{"error": "<message>"}`.

### B6. `serve_stdio`

- Reads `stdin` as **bytes**, one JSON-RPC message per line, decoded as UTF-8. On Windows
  the text-mode `sys.stdin` is the ANSI code page, so `main` passes `sys.stdin.buffer` and
  `sys.stdout.buffer`.
- Blank lines are skipped. A line that is not valid JSON or not valid UTF-8 answers
  `-32700` with `id: null`; `NaN`, `Infinity` and `-Infinity` are not JSON and are
  rejected the same way. A JSON array (a batch) answers `-32600` with `id: null`.
- Each response is `json.dumps(..., ensure_ascii=False)` encoded UTF-8 with
  `errors="backslashreplace"`, followed by `\n`, then flushed. A lone surrogate (legal in
  a JSON `\ud800` escape, not encodable as UTF-8) can only occur inside a JSON string, and
  there `backslashreplace` yields exactly its JSON escape — so no input can make encoding
  raise and end the loop. (Found in review: `"id": "\ud800"` previously killed it.) **Nothing else is ever written to stdout** — not a log line, not a warning.
  Diagnostics go to stderr.
- Returns at end of input. Requests are handled sequentially; a long synchronous call
  delays the next message. (Accepted for this version; async operations return 202
  promptly, so only synchronous operations block.)

## Test rules

- No GPU, Blender, ComfyUI or network beyond loopback.
- Drive `McpServer` through a `Transport` that forwards into the real app built by
  `create_app` (Starlette `TestClient` with `base_url="http://127.0.0.1:8765"`, adding the
  token header itself), so routing is exercised against the real route table rather than
  a fake one.
- `LoopbackTransport` is tested against a stdlib `http.server` on `127.0.0.1:0` in a
  thread: it sends the token, does not follow a 3xx, and ignores `HTTP_PROXY` /
  `http_proxy` set to an address that would fail.
- Required cases, at minimum: query parameters present in the manifest for all four asset
  operations; `/tool-manifest` equals `build_manifest(app)` and is not in the OpenAPI
  schema; every published MCP name carries the prefix and no published `inputSchema`
  contains `#/components/`; `create_asset` is absent; a path value `"../x"` stays inside
  its segment; a mutating call through the adapter succeeds with the token and the HTTP
  surface still refuses it without one; a 4xx is `isError: true` with the body intact; an
  async operation's 202 survives into the result text; an unknown tool is `-32602`; a
  notification gets no response; malformed JSON is `-32700`; `main` with no token returns
  `2` and writes nothing to stdout; no output anywhere contains the token.
