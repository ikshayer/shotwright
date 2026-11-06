# Contract — a failed job must say why (FR-27, invariant 7)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## The defect, with its evidence

The pipeline's first real failure was **undiagnosable**. `build_scene` failed and the entire
persisted record was this:

```json
{"state": "failed", "error_kind": "internal", "job_id": "5916c249...",
 "run_id": "ed5b55b0...", "shot_id": "SH1", "attempts": 1, "type": "scene"}
```

No message. No cause. No traceback. `log_path` exists as a field on `Job` and stayed
**null**.

The remediation hint returned to the caller was:

> "This is an orchestrator bug or unclassified failure; attach the job JSONL log
> (job_id/run_id) to a bug report."

**The hint tells the operator to attach a log that does not record why.** That is the whole
defect in one sentence.

Finding the actual cause required monkey-patching `JobError.from_exception` in a throwaway
script to print the traceback before it was swallowed. FR-27 exists precisely so that is
never necessary, and invariant 7 says never swallow an error. A classified kind with no
detail is a swallowed error wearing a label.

For the record, the cause was eight Pydantic validation errors on `RenderRequest` —
information that existed in memory, was classified into `internal`, and was then discarded.

## What must change

A failed job record must carry enough to diagnose it without re-running anything.

1. **The failure message is persisted.** `JobError` already has one; it must reach the
   journal.
2. **The underlying cause is persisted.** `JobError` carries `cause`; where one exists, its
   type and message must be recorded. A traceback, or a bounded excerpt of one, is better —
   decide and justify it, bearing in mind the journal is one line per record.
3. **`log_path` is populated** whenever a log exists, as a project-relative contained path.
   The field is already on `Job`; it has simply never been set. If a stage writes no log,
   say so rather than inventing a path.
4. **The remediation hint stays**, and must stop being a lie: once the log records the
   cause, "attach the job JSONL log" becomes true.
5. **A classified `internal` failure must include its origin** — which layer raised it.
   `internal` currently means "we do not know", and the operator cannot tell an adapter
   failure from a runner failure. The implementer who hit this said exactly that: *"The job
   record has no underlying cause or log path, so I can't confirm whether that was a real
   adapter failure or a runner failure."*

## Constraints

- **The journal is append-only and one line per record.** A record must stay a single line;
  `append_line` rejects embedded newlines, so a traceback must be escaped or bounded, not
  written raw. Do not change the journal's format or its precedence rule.
- **Every write goes through `ProjectRoot`** (FR-25), logs included. A log path must be
  contained and resolved through the helper.
- **`JobError` remains a closed enumeration** (FR-27). Do not add kinds to describe detail
  that belongs in fields; `internal` keeps its meaning and gains detail.
- **Do not leak secrets.** A message or traceback must never carry the loopback token or
  credentials. `Settings.credentials` exists; assume a message could brush it.
- **Bound the size.** A record carrying an unbounded traceback is a denial-of-service on the
  journal. Choose a limit, state it, and truncate loudly rather than silently.
- The state vocabulary is unchanged: `queued`, `running`, `completed`, `failed`, `cancelled`.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network.
- **The regression test is the defect above:** a job whose worker raises must produce a
  persisted record from which the cause can be read. Assert the message and the cause type
  are present — a test that only checks `error_kind` would pass today and is worthless.
- Required cases: a `JobError` with a cause persists both; a bare exception classified as
  `internal` persists its type and message; a very long traceback is truncated to the stated
  bound and the truncation is visible; the record stays a single journal line; a secret-like
  value in a message is not persisted verbatim; `log_path` is contained and relative.
- Assert the record is still **parseable** after all of this — a traceback containing quotes,
  backslashes and newlines is the obvious way to break one-line JSON.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1285 passed, 31 skipped**, ruff clean, about 152 s.

**The real acceptance criterion:** re-run the failing `build_scene` walkthrough and show
that the persisted record now names the cause. The parent will repeat that independently.

## Out of scope, and already diagnosed

Do **not** fix the `build_scene` failure itself. Its cause is known: nothing builds the
adapter's typed request, so the raw route payload reaches `RenderRequest` and fails
validation. That needs a composition layer and a `create_camera_profile` operation, and it
is a separate, larger piece of work. **Your job is to make the failure legible, not to make
it go away.** If you fix the underlying bug you remove the very evidence your tests need.
