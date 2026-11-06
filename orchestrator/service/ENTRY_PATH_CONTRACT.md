# Contract — the pipeline's entry path (FR-14, FR-19, AT-10)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The first end-to-end run died **two steps in, after 2.8 seconds**. `create_project`
succeeded; `draft_script` answered `missing_adapter: service.draft_script`.

Verified directly: the operation table declares **19** operations, and
`OperationContext.invoke` implements **9**, plus the four adapter-routed ones. These fall
through to `MissingAdapter`:

- `draft_script` — no way to create a `Script`
- `propose_shots` — no way to create `Shot`s
- `apply_typed_feedback` — unimplemented
- and **there is no episode-creation operation at all**

`revise_script` and `revise_shot` exist, but both require an existing record and its
mutation token. **So nothing can ever be created, and no adapter stage is reachable through
the required surface.** FR-14 forbids a hidden execution path, so seeding records behind the
API is not a workaround.

Nine rounds of contract work and 1233 passing tests did not catch this, because
`tests/api/test_adapters.py` only asserts behaviour for the seven operations on its
adapter-absent list, and these are on neither that list nor any implemented path. Two of
this project's own amendments made it worse by enumerating the surviving `501` set in a way
that *implied* `draft_script` and `propose_shots` worked.

## The decision that shapes this: the agent authors, the orchestrator persists

**`draft_script` and `propose_shots` persist agent-authored content. They generate nothing.**

This is a creator decision, and the reasoning is binding because it constrains the design:
there is **no text LLM in §8.2's model list and no slot for one in §8.3's serialization
chain**. The architecture puts the intelligence in the orchestrating agent — the PRD is
explicit that an AI coding agent acts as production coordinator, and `references.sample`
sets the precedent that the orchestrator returns *data for the agent to inspect* rather than
interpreting it itself. A generating endpoint would require naming and budgeting a model
that does not exist.

So these are **store/service operations**, not adapters. The operation table marking them
"adapter absent" was wrong, and that marking is what hid the gap.

## What to build

Three operations, reachable from **both** HTTP and CLI, as AT-10 requires:

| Operation | Method and path | Behaviour |
|---|---|---|
| `create_episode` | `POST /projects/{project_id}/episodes` | Persist an agent-authored `Episode` at version 1 |
| `draft_script` | `POST /projects/{project_id}/episodes/{episode_id}/scripts/draft` | Persist an agent-authored `Script` at version 1 |
| `propose_shots` | `POST /projects/{project_id}/episodes/{episode_id}/shots/propose` | Persist agent-authored `Shot`s at version 1 |

`draft_script` and `propose_shots` keep their existing routes; only their behaviour changes.
`create_episode` is new, so **the operation table grows and AT-10's enumeration changes** —
amend `orchestrator/api/CONTRACT.md` accordingly.

### Rules these must obey

1. **Creation, not revision.** Each persists a **new** record at version 1. Revising an
   existing one is `revise_script` / `revise_shot`'s job and must stay there. Creating over
   an existing record is a conflict, not an overwrite — the store already raises
   `VersionConflict` with `expected` 0 when a record exists, and that maps to `409`.
2. **Enter through the service boundary.** `CONCURRENCY_CONTRACT.md` requires mutating work
   to hold the per-project command lock, and store methods alone do not orchestrate.
   Creation does not invalidate anything downstream — there is nothing downstream yet — so
   it must **not** run a cascade, but it must not bypass the boundary either.
3. **`expected_version` is the advancing mutation token**, never a content version. A
   creation supplies the token for a record that does not exist yet; use what the store
   already defines for that case rather than inventing a convention.
4. **Validate the payload as the domain entity.** `Episode`, `Script` and `Shot` already
   carry their own validators and `Script` has a model validator; call them, do not restate
   their rules. A rule appearing in this layer is a defect.
5. **Identifiers in the URL must match the body**, exactly as `revise_shot` now enforces —
   a `POST` to `.../episodes/E1/...` carrying `episode_id: E2` is `400 request_mismatch`.
6. `propose_shots` accepts **a list** and persists each shot. It is append-only: proposing
   again adds shots, it does not replace the set. Partial failure must not leave half a set
   silently — report what was and was not written.

## Also required: close the hole that hid this

`tests/api/test_adapters.py` asserts behaviour for seven operations out of nineteen.

**Every operation in the table must be asserted to be either implemented or deliberately
`501`**, driven from the route table rather than a hand-written list — the same way the
parity test is. After this work the `501` set is exactly `search_assets`, `create_asset`,
`sample_references`, `export` and `apply_typed_feedback`; everything else must answer
something other than `missing_adapter`. A new operation that is neither implemented nor
listed should fail that test.

This is the single most valuable test in this pass. Without it the next gap hides just as
long.

## Out of scope

`apply_typed_feedback` stays `501` — it belongs with FR-10's review surface. Sound is
Phase 2 by creator decision. The review UI follows Phase 1. Adding a shot to
`generate_media`'s route (outputs are shot-scoped) is a separate change already decided.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network. These are store/service operations.
- Required cases: each operation creates and the record is readable afterwards; creating
  twice conflicts rather than overwriting; a URL/body identifier mismatch is `400`; an
  invalid payload is `422` from the entity's own validators; `propose_shots` with several
  shots persists all of them and is append-only; the command lock is held for creation; and
  **API/CLI parity for all three**, driven from the route table.
- One case must assert the **whole table** is covered, as described above.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1233 passed, 30 skipped**, ruff clean, about 160 s.

**The real acceptance criterion is the pipeline, not the suite.** When this lands it must be
possible to drive project → episode → script → shots entirely through the CLI, and reach
`build_scene`. The parent re-runs the Phase 1 attempt to confirm it.

## Amendment 1 — adjudication, and the gap that matters most

The implementer and the independent test author **agreed on the first combination** —
**1285 passed, 31 skipped**, ruff clean, 151.84 s. They also chose the same request
envelopes and the same resolution for the Episode versioning problem without consulting
each other, which is what a settled contract produces. That is now twice in a row.

**The entry path is open.** The implementer drove the real Click CLI end to end:

```
create-project -> create-episode -> draft-script -> propose-shots -> build-scene
```

and `build-scene` submitted a job that reached `queued`, `running`, `failed`, returning
`job_error` with kind `internal` — **not** `missing_adapter`. The pipeline is reachable
through the required surface for the first time.

### Ratified

- **Request envelopes are `{"episode": ...}`, `{"script": ...}`, `{"shots": [...]}`.**
  Both agents chose these independently; they match `ScriptRevision`'s existing shape.
- Creation uses the store's defined creation token, **0**. Scripts and shots go through
  `store.create()`; episodes through `store.save(..., expected_version=0)`.
- Creation enters the service boundary under `store.command()` and runs **no** invalidation
  cascade, as required.
- The `501` set is exactly `search_assets`, `create_asset`, `sample_references`, `export`,
  `apply_typed_feedback`.

### The whole-table guard now exists, and it is the most valuable thing here

`tests/api/test_entry_path.py::test_every_route_is_implemented_or_deliberately_501`
derives its cases from `operations(app)` and compares the observed `501`s against the five
deliberate absences. **A newly registered operation that falls through to `MissingAdapter`
now fails it automatically.**

This is the hole that let a missing entry path survive nine rounds of contract work and
1233 passing tests: `test_adapters.py` asserted behaviour for seven operations out of
nineteen, and the two broken ones were on neither its list nor any implemented path. Two
of this contract's own amendments then enumerated the surviving `501` set in a way that
*implied* they worked. Do not replace this test with a hand-maintained list.

### Corrected: "at version 1" was wrong for Episode

This contract said all three records are persisted "at version 1". The test author caught
that **`Episode` inherits `Entity`, not `VersionedEntity`**, and has no content `version`
field at all; both agents resolved it the same way and both reported it.

**Binding:** `Script` and `Shot` are persisted at **content version 1**. An `Episode` has no
content version; what holds for it is that its **first store mutation token is 1**. Do not
add a content version to `Episode` to make the sentence true — the asymmetry is in the
domain by design, and the contract was simply sloppy about it.

### Still open, and the first one is urgent

- **A failed job records no cause and no log path.** `build-scene` failed with kind
  `internal` and the implementer could not tell whether that was an adapter failure or a
  runner failure, because the job record carries neither a message nor a `log_path` —
  despite `Job.log_path` existing as a field. FR-27 exists so that a failure names what to
  do about it, and right now the pipeline's first real failure is **undiagnosable**. This
  must be fixed before the Phase 1 run is retried; otherwise the run teaches us nothing.
- **There are no read routes for episodes, scripts or shots.** The API offers
  `load_project` and nothing else, so the test author had to verify post-create readability
  through `ProjectStore` directly. An agent or UI can create a script and then has no way to
  fetch it back. FR-10's review surface will need these.
- **Partial shot-proposal failure is implemented but untested.** The implementer reports
  written and unwritten shot ids; the test author declined to cover it because this contract
  never defined rollback behaviour, a response shape, or a failure-injection boundary.
  Define those before relying on it.
