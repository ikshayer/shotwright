# Contract — API, CLI and tool manifest (FR-14, FR-25)

Written by the orchestrator. Implementation and tests come from **different agents
working in parallel from this file**, neither reading the other's output. Where they
disagree, this file decides and the disagreement is reported, never patched away.

## What this layer is

A thin, typed surface over the existing service layer. It owns transport, validation,
authentication and error mapping — **and no business rules**. Anything that is a rule
already lives in `orchestrator/domain/`; anything that spans store and domain lives in
`orchestrator/service/`. A rule appearing here is a defect, and that exact drift was the
previous pass's finding.

The UI must use this same backend. FR-14 is explicit that there is no separate hidden
execution path, and AT-10 tests it.

## Operations (normative — AT-10 tests this enumeration)

Every operation below must be reachable from **both** the HTTP API and the CLI.

| Operation | Backed by |
|---|---|
| create / list / load project | `store` |
| draft script, revise script | `service.dependencies.revise_script` |
| propose shots, revise shot | `service` |
| search assets, create asset | *adapter absent* |
| build scene | *adapter absent* |
| render preview | *adapter absent* |
| generate media | *adapter absent* |
| sample references | *adapter absent* |
| report jobs | `jobs.queue` |
| apply typed feedback | `service` |
| score rubric | `domain.entities.RubricScores` |
| export | *adapter absent* |

**Operations whose adapter does not exist yet are still routed, typed and documented.**
They return a `501`-shaped error naming the missing adapter. They are NOT omitted:
AT-10 requires the enumeration to be complete, and the UI is built against it. An
endpoint that silently does not exist is worse than one that says what is missing.

## Security (FR-25, AT-17)

- Bind `127.0.0.1` by default; the bind address comes from `Settings`, and a
  non-loopback value is a startup error, not a warning.
- **Deny cross-origin requests.**
- **Every mutating call requires the loopback token.** Reads do not. A missing or wrong
  token is `401`, and the response body never echoes the token or any secret.
- Every filesystem write goes through `platform.containment.ProjectRoot`. The API adds
  no second path check — that duplication was a real vulnerability in the store and
  must not reappear.
- Uploads validate extension **and** magic bytes, with §6.1 size caps; a rejection names
  the limit and the observed value.

## Error mapping

The only place this translation happens:

| Raised | HTTP |
|---|---|
| `VersionConflict` (store) | **409**, body carries expected, actual and the current head |
| `TokenConflict` / stale mutation token | **409** |
| `RecordNotFound` | 404 |
| `ContainmentError`, `UploadRejected` | 400, naming the offending field |
| `SettingsError` | 500 at startup only; the server must refuse to start |
| `TransitionError`, `ReviewApprovalError` | 422, carrying the refusal reason verbatim |
| `JobError` | 409 if retryable, 422 if terminal; body carries `kind`, `retryable` and the **remediation hint** |
| missing adapter | 501, naming the adapter |

Error bodies are a single consistent shape. The remediation hint is the point of
FR-27's taxonomy — it must survive to the client, not be flattened into a message.

## CLI

Same operations, same names, over the same functions the API calls. It must be usable
non-interactively by an agent: machine-readable output (`--json`), a non-zero exit code
on failure, and errors on stderr. The CLI must not reach into the store directly.

## Tool manifest

Generate, do not hand-write:
- the OpenAPI schema from the FastAPI app;
- a tool-definition manifest for agent consumption, derived from the same route table.

A new operation must appear in both without a second edit. Two hand-maintained lists
would be the duplicated source of truth this project forbids.

## Test rules

- No GPU, no Blender, no ComfyUI, no network. Adapter-backed routes are tested for
  their 501 contract, not their behaviour.
- Required negative cases: a non-loopback origin refused; a mutating call without the
  token refused; a stale mutation token returning 409 with both values; a crafted path
  rejected; an upload whose magic bytes contradict its extension rejected; an oversize
  upload reporting limit and observed; a terminal `JobError` carrying its remediation
  hint through to the response body.
- At least one test asserts **API/CLI parity**: every operation in the table above is
  reachable from both, driven from the route table rather than a hand-written list, so
  adding an operation cannot silently skip one surface.

## Amendment 1 — the interface, pinned

The first pass specified behaviour but not names, so the test author had to assume an
interface while the implementer chose its own. That was a defect in this contract, not
in either agent. The assumptions below were reached independently and are sensible, so
they are now binding. **Implementation conforms to these; tests do not move.**

```python
orchestrator.api.app.create_app(settings: Settings, token: str) -> FastAPI
orchestrator.api.cli.app                      # Typer/Click application object
orchestrator.api.manifest.build_manifest(app) -> dict   # tools each carrying "name"
```

- Loopback token header: **`X-Loopback-Token`**.
- CLI command names follow their route function, with `_` and `-` both accepted.
- A request from a foreign origin is **403**, not 400. 400 means malformed; a refused
  origin is a policy decision.
- A 501 body carries an **`adapter`** key naming the missing adapter as a string.
- Every mapped error body shares one top-level shape, carrying `kind` and `retryable`,
  and — where the exception provides them — `expected`, `actual`, `current`, `limit`,
  `observed` and `remediation`.
- Every route with `include_in_schema=True`, health included, needs a CLI twin.

### `TokenConflict` does not exist — remove it

The error table listed `TokenConflict` alongside `VersionConflict`. There is no such
type. The store carries `mutation_token` and raises **`VersionConflict`** for both a
stale content version and a stale mutation token. The API maps that one exception to
409 and distinguishes the two cases **in the body**, not by type.

The API layer must NOT define its own exception class for this. Declaring a type the
store already owns puts policy in the transport layer, which this contract forbids in
its second paragraph, and creates the duplicated source of truth the project keeps
being bitten by. Import from the store; do not restate.

## Amendment 2 — Host validation (DNS rebinding)

Found by security review of the first implementation. It is a real hole, not a
theoretical one, and it must be closed in the conformance pass.

The origin middleware checks `Origin` **only when the header is present**, and nothing
validates `Host`. A cross-site simple `GET` carries no `Origin`, and reads require no
token. So a page on `attacker.com` whose DNS is re-pointed to `127.0.0.1` reaches this
API, and the browser treats it as same-origin, meaning CORS never engages. Binding to
loopback does not prevent this: the attacker's own browser is on loopback.

Binding requirements:

- **Validate `Host` against a loopback allowlist** on every request: `127.0.0.1`,
  `localhost`, `[::1]`, each optionally with the configured port. A `Host` outside it is
  **403**, before routing and before any handler runs. Starlette's `TrustedHostMiddleware`
  is acceptable if configured from `Settings` rather than hard-coded.
- **A missing `Origin` is not a pass.** Absence must not route around the check; the
  `Host` check is what makes absence safe.
- The check applies to **reads as well as mutations**. Reads are unauthenticated by
  design, which is exactly why they are the exposed surface here.

Tests must cover: a `Host` of `attacker.com` refused with 403; a rebinding-shaped request
with a foreign `Host` and **no** `Origin` refused; `127.0.0.1`, `localhost` and `[::1]`
accepted, with and without the port; and the refusal happening on a **read**, not only
on a mutating call.
