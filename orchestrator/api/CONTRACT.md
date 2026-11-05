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

## Amendment 3 — the conformance pass, and what it found

Written by the orchestrator after adjudicating the implementation against the tests.
Everything here is binding, and the reasons are recorded because each one was a defect
in this contract rather than in either agent.

### The operation table omitted the upload surface it depends on

The Security section requires uploads to be validated by extension **and** magic bytes
with the section 6.1 caps, the Test rules require four negative upload cases, and AT-17
names them again — but the operation table listed no operation that can carry a file.
The test author, writing from this contract, correctly concluded that one must exist and
failed the suite with "contract requires validated uploads but the app exposes no
multipart route". The first implementation instead accepted base64 in a JSON body, which
no test could discover and which inflates a 500 MB video cap by a third.

**Binding:** `create_asset` is the multipart surface. It takes an optional repeated file
field, so the operation enumeration is unchanged:

- Upload validation is transport-level and runs **before** the missing-adapter check. A
  malformed upload is therefore `400` and a well-formed one is still `501`.
- The file field is **optional**. A required one would make an adapter-absent operation
  answer `422` to the 501 test, and an `UploadFile | None` annotation publishes
  `anyOf[binary, null]`, which hides the field from any consumer reading the schema.
- Validation calls `platform.containment.validate_upload`. The API adds no second check.

**Still open:** the PRD makes creator-supplied **reference** uploads P0 (FR-17 images,
FR-17b local video) and this table has no operation for them. `create_asset` is the
asset path, not the reference board. Add the reference operation when FR-17 is built;
do not quietly widen `create_asset` to mean both.

### `TrustedHostMiddleware` is NOT acceptable — Amendment 2 was wrong to offer it

Amendment 2 permits Starlette's `TrustedHostMiddleware` "if configured from Settings".
It cannot satisfy Amendment 2's own requirements, confirmed against the installed
Starlette 0.52.1:

1. It matches on `headers["host"].split(":")[0]`, which reduces both `[::1]` and
   `[::1]:8765` to `"["`. An IPv6 loopback literal can never match it, yet Amendment 2
   requires `[::1]` to be accepted.
2. It answers `400`. Amendment 2 requires `403`, and for good reason: a refused host is
   a policy decision, not a malformed request.

**Binding:** validate `Host` in the application's own middleware, against an exact
allowlist built from `Settings` — the three loopback names, each bare or with the
configured port. The check runs on every request before routing, reads included.

### Error mapping has one implementation, not one table restated twice

This contract already said error mapping is "the only place this translation happens",
and the first implementation had it twice: the HTTP handlers in `app.py` and a parallel
copy in the CLI. The copy had already drifted — it dropped `limit_name` from a rejected
upload, dropped the offending path from a containment error, and reported Python class
names where HTTP reported stable codes.

**Binding:** `orchestrator/api/errors.py` owns the exception-to-`(status, body)` mapping
and the body shape. Every surface maps through it. `MissingAdapter` lives there too,
because the 501 it produces is part of the error contract. An exception the table does
not cover returns `None` and stays an unhandled 500: it is a bug, not a request failure,
and must not be dressed up as one.

### Mutating request bodies are required

An optional body publishes `anyOf[model, null]`, which erases the model's own fields
from the route's schema — invisible to the generated manifest, to the OpenAPI document
and to any test that reads either. A mutating operation's body is therefore declared as
the model itself. Non-mutating operations keep an optional body.

### The manifest is keyed on the route name and never reads a cached schema

`app.openapi()` memoises into `app.openapi_schema`, so a manifest built during
`create_app` froze the schema and silently omitted every later route. `build_manifest`
now regenerates from `app.routes`, and nothing caches a copy on `app.state`.

Tool names come from `route.name`, not `operationId`: FastAPI only honours an explicit
`operationId`, deriving the rest from name, path and method together, which would
publish a tool called `brand_new_operation_brand_new_operation_get`.

### The CLI must not be run through `click.testing.CliRunner`

`main()` invoked the production CLI through Click's **test** harness. In Click 8.2+ that
captures stderr separately from `result.output`, so routing output by exit code — as it
did — discarded every error message. It also returned `0` for a failed operation,
because with `standalone_mode=False` Click *returns* a `click.exceptions.Exit` status
instead of raising it. Both are fixed; both were invisible to the suite, which exercises
`cli.app` directly and never `main()`.

**Binding:** the CLI writes to the real streams, stdout stays machine-readable, errors go
to stderr, and a failed operation exits nonzero. An upload operation gets a repeatable
`--file`, so every operation is genuinely reachable from both surfaces and not merely
present as a command name.

### A harness assumption that OpenAPI 3.0 would be emitted

`_file_field` in `tests/api/test_uploads_and_paths.py` discovered a file field by
`format: "binary"`, the OpenAPI **3.0** spelling. FastAPI emits OpenAPI **3.1**, which
spells it `contentMediaType`. Discovery was widened to accept both. No assertion moved:
before the change the oversize, magic-byte, extension and containment cases could not
run at all.

### Known limit, unresolved

Starlette's `UploadFile` spools a request body above its threshold to an OS temp file,
outside `platform.containment.ProjectRoot`. Invariant 4 admits no exceptions, "including
temp files and logs". Nothing is persisted today because the adapter is absent, but the
spool file is still written. Resolve this when the asset-creation adapter lands, by
streaming the body to a contained path. Do not record this invariant as satisfied.

## Amendment 4 — adjudication of the independent review

A read-only review of the conformance pass returned eleven findings. All were
reproduced before acting; the corrections are binding, and two of the review's points
corrected Amendment 3 itself.

### Authentication runs before the body is read

Authenticating inside the route handler was too late: the framework reads and parses the
body first, so an unauthenticated request with malformed JSON answered `422` and an
unauthenticated multipart request had its parts **spooled to disk** before the token was
ever examined. The token is now checked in the same middleware as `Host` and `Origin`,
before routing.

That middleware cannot consult the operation table, so it keys on the HTTP verb.
`create_app` therefore asserts at startup that every operation's `mutating` flag agrees
with its method, and refuses to build an app where they disagree. Keep them in step.

A consequence worth knowing: an unauthenticated request using a mutating verb on a
read-only path answers `401`, not `405`. That is deliberate — the set of supported
methods is not disclosed to an unauthenticated caller.

### The URL must identify the record being changed

`revise_shot` and `revise_script` took their target from the body and ignored the
identifiers in the path, so a POST to `.../shots/A/revise` carrying shot `B` revised
`B`. The URL was decorative, and any log, audit trail or future authorisation placed on
the path would have described the wrong record. A body that contradicts its own URL is
now `400 request_mismatch`, carrying the field and both values. This is request
validation, not a business rule: it decides nothing about the record.

### Uploads are no longer buffered, and `observed` is the real size

The previous pass read each file up to the largest configured cap. With several parts
that multiplied, an invalid first file did not stop later reads, and a file above the cap
reported `observed` as the truncated read length — a 112-byte file with a 20-byte cap
reported `21`.

**Binding:** validation needs only the magic-byte header and the true size, so that is
all `UploadedFile` carries. The size comes from the multipart parser, which counted the
bytes as it wrote them. Memory is bounded by the 64-byte header per part regardless of
upload size, and `observed` is exact. The storing adapter, when it exists, must stream
the body to a contained path rather than accept bytes through this object.

### Pixel and duration caps are NOT enforced, and must not appear to be

The first pass accepted `image_long_edge_px` and `duration_seconds` as form fields and
checked the caps against them. That validates nothing: a caller wanting past the cap
omits or understates the value. A 10,000x1 PNG passed. The fields are removed, because a
cap that trusts the caller is worse than a cap that is openly absent.

Extension, magic bytes and the **byte** caps are enforced. The section 6.1 pixel and
duration caps are **not** enforced anywhere today; they need a decoder and belong with
the adapter that decodes the file. Do not record AT-17 as fully satisfied until then.

### The API cannot classify a version conflict, and must not guess

Amendment 1 asked the 409 body to distinguish a stale content version from a stale
mutation token. The API cannot: both `expected` and `actual` are mutation tokens. The
attempt to infer it by comparing `current.version` against `expected` compared a content
version against a token and misclassified ordinary token conflicts.

**Binding:** the body reports `expected`, `actual` and `current` faithfully and claims
nothing further. **The store owns this distinction and must surface it on
`VersionConflict`** before any surface can report it. Until then Amendment 1's
requirement is deliberately unmet, and that is recorded rather than faked.

### Security headers are validated exactly once each

Only the first `Host` and `Origin` were examined, so a crafted request could append a
second. Exactly one `Host` is now required — HTTP/1.1 permits only one — and more than
one `Origin` is refused. A malformed `Origin` such as `http://[` also produced a `500`,
because `urlsplit` sat outside the guard that caught its error; it is now `403`.

### Framework failures share the one error shape

A 404, a 405 and a missing multipart boundary answered Starlette's `{"detail": ...}`,
so a client had to parse two formats from one API. Failures the framework has already
classified are now emitted in the shared shape. An **unmapped exception** is still a
plain `500`: that is a bug to fix, and dressing it up as a tidy error body would hide it.

### The CLI validates what the HTTP route validates

CLI commands never checked the operation's request model, so a missing field surfaced as
a bare `KeyError` where HTTP returned a typed validation failure. The CLI now validates
`--data` against the same model from the same registry.

### The CLI had no entry point at all

`main()` returned a status that nothing acted on: there was no `__main__`, no
`[project.scripts]`, and `python -m orchestrator.api.cli` emitted a runpy
`RuntimeWarning` onto stderr because the package imports `cli` eagerly. The CLI was not
runnable by the agent the contract requires it to serve. The entry point is
`orchestrator/api/__main__.py`, so `python -m orchestrator.api` works, and
`[project.scripts]` exposes `cctv-pipeline`. `click` and `python-multipart` are now
declared dependencies; both were previously only transitive.

### Corrections to Amendment 3

- **Invariant 4 is violated today, not merely at risk.** Starlette spools a multipart
  part above 1 MB to an OS temp file outside `ProjectRoot`, and no multipart route can
  avoid it with the default parser. Authentication moving ahead of body parsing means
  only an *authenticated* caller can now cause such a write, which narrows the exposure
  but does not close it. Closing it needs a parser that streams through the containment
  helper. Until then this invariant has a known, documented exception — the only one.
- **`anyOf` does not "erase" a model's fields.** They remain reachable by following the
  `$ref` inside the `anyOf`. What an optional body actually does is put the model behind
  a union that naive consumers do not traverse, which is why the generated manifest and
  the test harness both failed to see the fields. The decision stands; the reasoning in
  Amendment 3 was overstated.

### A test assertion that was weaker than this contract

`test_security.py::test_non_loopback_origin_is_refused` accepted `400` or `403` for a
refused origin. Amendment 1 requires `403`. Tightened to `403` — a test may not be more
permissive than the contract it exists to enforce.
