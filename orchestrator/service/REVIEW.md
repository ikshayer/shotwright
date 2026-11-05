# Service review and adjudication - 2026-11-04

Baseline for this pass: b386a28, 560 tests. Parent authored
CONCURRENCY_CONTRACT.md, dispatched separate implementation and test authors through
codex exec, reviewed their source, reran the suite/probes, and adjudicated findings.
All changes are uncommitted for the parent session to inspect and commit.

## Closed: SVC-01, SVC-02 and SVC-03

| Finding | Prevention and independently checked evidence |
|---|---|
| SVC-01: lost lifecycle updates | Every successful entity write advances a mutation token, distinct from content version. load_with_version returns the token from the same locked snapshot. update/revise/save reject a stale expected_version with VersionConflict carrying current state for future HTTP 409 mapping. Two snapshots cannot both overwrite notes. Lifecycle, no-op and revision writes, historical-head rejection, reopen and two-store/thread cases are covered. |
| SVC-02: stale Shot under an approved gate | A durable immutable cascade plan precedes entity writes. All affected gates are revoked before upstream append or Shot state changes. Failure leaves a pending plan; all service approvals/revisions refuse new work until explicit resume_invalidations rolls it forward. Reopen and standalone copied-project tests exercise failure at plan, both gates, upstream, both stale writes and completion; repeated failure and repeated recovery preserve artifacts and append upstream only once. Current stale/draft Shots also block gate reapproval. |
| SVC-03: approval escaping invalidation | Approval, revision and recovery commands hold the same resolved-project lock across reads and commit. Same-thread nested service commands fail explicitly; other threads serialize. Domain dependency guards revalidate immediately before commit. Durable all-state impact fences prevent review/draft content from being approved after a completed upstream revision; approval first instead causes staleness. Persisted Reviews bind to content versions throughout. |

Original reproduced failures at b386a28: two content-v1 lifecycle writes erased
feedback; a second-stale-write failure left Location v2 and a stale Shot under gate
cut 3; approval injected before Location commit escaped the old candidate snapshot.
The superseding probes assert those outcomes are prevented rather than reproduced.

## Persistence, domain ownership and recovery

Content revisions retain version/supersedes. Lifecycle writes append immutable
vNNNN.lifecycle.TOKEN.json snapshots; they do not replace content files. Mutation
metadata has a predecessor token, validated within and across content versions.
Lifecycle reads reuse the existing lifecycle-only change/transition guard, rejecting
content/artifact corruption and illegal edges. Update payloads are Pydantic-validated
after CAS before append. Rejected writes leave bytes and tokens unchanged.

Legacy schema-1 bases without metadata remain readable, including multi-version
histories whose final lifecycle state was stored in place. Their synthetic token is
the content version. New tracked successors must continue that token, and a legacy
file cannot regress a newer persisted lifecycle token. Reads never rewrite history.

_journal/cascades contains immutable plans and completion markers. Completed plans
remain dependency-freshness evidence and are validated by the service before use.
The store validates journal structure; the domain owns revision, approval, Script
impact and CameraProfile classification rules; service coordinates I/O and recovery.
All new production writes use the existing ProjectRoot/atomic-write boundary.

Recovery is explicit: resume_invalidations(store) returns completed plan IDs and
then returns () on another call. Exact committed successors and already stale Shots
are skipped. Divergent content, invalid plans or persistent failures fail closed,
retain evidence, and report cause/progress/remediation. No automatic plan deletion,
rollback or approval inheritance exists. Store primitives remain persistence APIs;
future mutating API commands must use the serialized service boundary.

## Review findings and adjudication

The first independent suite exposed four failures rather than validating the new
implementation's assumptions. Subsequent independent hardening exposed token
regression and seven malformed completed-plan variants. Read-only gpt-6.1-sol reviews
also reproduced wrong Shot key selection, unchanged mappings missed by edited beats,
late path validation, incomplete history checks, lifecycle content corruption and
invalid lifecycle input. Parent accepted these findings and sent corrections to
implementation agents; independent test authors added regressions.

Corrections preserve populated Shot location/camera links, reuse the existing Script
selector, validate revision payloads/containment before recording plans or revoking
gates, retain StyleBible revision support, validate completed plans and lifecycle
history, and preserve exception causes. Post-only/no-op profile and Script metadata
changes preserve gates and create no invalidation fence. Returned changed IDs name
only approved-to-stale transitions. Prior boundary-repair tests remain substantive.

Parent rejected the test author's broadened dependency-race assertion accepting
VersionConflict with equal target tokens: changed dependency evidence must rerun the
domain freshness guard and reject as INVALID_MANIFEST. The precise assertion was
restored; target-token races still require VersionConflict. Earlier suspicions about
post-only gate revocation, marker filename matching and Iterable annotations were
rejected against the corrected source and probes; those cases had already been fixed.

## Independent final verification

- `.venv\Scripts\python.exe -m pytest -o addopts="" -q`: **716 passed, 5 warnings in 83.40s**. Warnings are from intentionally malformed Pydantic payloads.
- `.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise`: **All checks passed!**
- git diff --check: clean.
- _work/service-review/reproduce_consistency.py: SVC-01/02/03 closed=true;
  all_closed=true. It checks retained feedback/stale-token rejection, gate-first
  partial failure, reapproval refusal, idempotent recovery, both approval/revision
  orderings, nested-command refusal and same-root threaded serialization.
- 156 new cases were written by agents other than every production implementer. They
  use real JSON, persisted Reviews, deterministic interleavings and injected errors.

## Still open / not verified

No SVC-01..03 finding remains open within the single-writer contract. Actual HTTP
409 responses await the API layer; only the boundary's VersionConflict data/type is
tested here. Cross-process writers are unsupported, not tested. Physical power loss,
forced process termination, real disk exhaustion and OS-level crash durability were
not verified; reopen/copy recovery uses deterministic injected failures on real JSON.
No GPU, Blender, ComfyUI, FFmpeg adapter or finished-shot acceptance test was run.
Long-history performance was not benchmarked. Broader narrative policy for logline
and beat-order changes remains outside this repair, as in the prior CONTRACT.md.

## Retained prior boundary-repair result

The preceding pass moved check_review_approval and Script impact selection into the
domain, persisted real Reviews and proved mapping-only invalidation with retained
history/artifacts and standalone reopen. It resolved policy placement, not the three
consistency defects above. That distinction and its regression assertions are kept.
