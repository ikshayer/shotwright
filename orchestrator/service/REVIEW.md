# Service review and adjudication - 2026-11-04

Read-only review: gpt-6.1-sol. Implementation: gpt-6-luna. Tests: a separate agent
from CONTRACT.md, without reading the concurrent production changes. The parent
orchestrator inspected the diff, reran verification and adjudicated the findings.
Initial review used source at commit 2907631; production fixes remain uncommitted.

## Resolved in this bounded repair

1. Approval binding policy was defined in service/approval.py. The comparisons were
   correct, but belonged to the domain. domain/state.py now exposes
   check_review_approval and ReviewApprovalError. Service retains loading, persistence
   and INVALID_MANIFEST mapping, including the original cause.
2. Script beat-change scope was defined in service/dependencies.py. Domain now owns
   shots_invalidated_by_script. It handles beat addition/removal/content edits, mapping
   changes and old/new mapped Shot IDs; dependencies and Shot.beat_id also link Shots.
   Selection is approved-only, episode-scoped, stable and deduplicated.
3. HIGH: mapping-only revisions previously committed without invalidating anything.
   The new real-store integration test demonstrates staleness of old/new mapped Shots,
   retained artifacts and Script history, gate-C revocation, and reopening from JSON.

The first HANDOFF description called approval matching a reimplementation; source
review found no pre-existing domain matcher. This was misplaced policy, rather than
an inconsistent duplicate executable rule. No approval behavior relaxation was needed.

The parent's first implementation inspection found repeated input Shots could produce
repeated IDs, plus an outdated state.py docstring. The implementer corrected both;
a separate test author added the repeated-input ordering regression. Final independent
review found no blocking findings within CONTRACT.md's scope.

## Open HIGH findings - address before adapters

### SVC-01: lifecycle writes have no advancing concurrency token

Evidence: load two review-state Shot snapshots, both content version 1. Save
approval_notes="creator feedback" through the first. Approve using the older snapshot
and expected_version=1. Both writes succeed; notes become empty and version stays 1.

Location: store/json_store.py update(), _head_checked(), _read_record(); approval.py
read/modify/write orchestration. PRD 10.0 and AT-12 require conflicts instead of lost
updates. The next contract must distinguish an advancing mutation token from the
content version to which Review binds. Merely serializing individual writes is
insufficient when stale snapshots still carry an accepted token.

### SVC-02: partial cascades can leave a stale Shot under an approved gate

Evidence: two approved Shots depend on Location hall; Episode gate C approves cut 3.
Inject failure on the second stale write. Reopen JSON: hall is version 2, first Shot
is stale, second remains approved, and Episode still has approved_cut_version=3 and
approval_review_id=gate. The raised JobError reports shots stale=[s1], gates revoked=[].

Location: service/dependencies.py _invalidate(). revise_shot() has the analogous
append-draft-before-gate-revocation ordering. PRD 10.2/AT-12 require fresh gate C once
a Shot becomes stale or draft. Conservative gate-first revocation is a bounded
improvement, but does not provide atomicity, recovery or protection from reapproval.
Define failure/recovery semantics with independent fault-injection tests.

### SVC-03: approval can escape an upstream invalidation snapshot

Evidence: snapshot a dependent Shot in review; inject real approve_shot immediately
before the Location revision commits. revise_location returns no changed Shots.
Reopen JSON: Location is version 2 and the dependent Shot remains approved.

Location: service/dependencies.py revise_script/revise_location/revise_camera_profile
candidate snapshot and commit boundary. PRD 10.2/AT-12 require invalidation of affected
approved Shots. Define command-level serialization and dependency revalidation across
approval/revision operations. Moving the snapshot alone leaves a race window.

## Verification and limits

The parent inspected and independently reran all three deterministic reproduction
probes successfully after the boundary repair. Disposable probe source is
_work/service-review/reproduce_consistency.py; it uses a unique contained project
root per run. Each probe used real ProjectStore JSON; Reviews were persisted.
These demonstrate deterministic interleavings and an injected OSError, not actual
thread scheduling, a physical disk failure or crash-recovery guarantees.

Baseline: 527 tests passed, Ruff clean. Standard pytest temporary-root discovery was
denied by this session's sandbox. Redirecting TMP, TEMP and PYTEST_DEBUG_TEMPROOT to
a ProjectRoot-created _work/pytest-takeover directory let the full suite run.
Final suite/lint results are recorded in HANDOFF.md. No GPU, Blender, ComfyUI, FFmpeg
adapter or finished-shot acceptance test was run for this CPU-only boundary repair.

Logline-only and beat-order-only revisions preserve prior behavior (no invalidation),
as explicitly scoped in CONTRACT.md. Broader narrative scope policy is not settled
by these tests. Existing PRD example/normative-storage discrepancies in HANDOFF remain
unmodified; this repair does not change store concurrency or append-only semantics.
