# Contract - service/domain boundary repair

This bounded repair completes the first HANDOFF item. Implementation and tests are
written by different agents from this contract. Review/test agents report rather
than fixing production code. PRD sections 10.1, 10.2 and FR-19/AT-12 govern it.

## Approval rule ownership

Extend existing domain/state.py (do not create a parallel approval module):

    class ReviewApprovalError(ValueError): ...
    def check_review_approval(review: Review, *, target_id: str,
                              target_version: int) -> None: ...

The pure guard succeeds only when Review.target_id and Review.target_version exactly
match the requested target and its approval_state is ShotStatus.APPROVED. Every
other case raises ReviewApprovalError with a useful target/version or state reason.
It performs no I/O and mutates no record. It serves Shots, StyleBibles and Episode
cut versions, which have the same binding rule. Existing transition behavior stays.

service/approval.py owns loading the Review and mapping missing records and domain
guard failures to JobError(INVALID_MANIFEST), preserving the cause. It delegates all
target/version/opinion comparisons to the domain guard. Existing public approval
service signatures, expected_version behavior and persistence layout stay unchanged.

## Script invalidation rule ownership

Extend existing domain/invalidation.py:

    def shots_invalidated_by_script(before: Script, after: Script,
                                    shots: Iterable[Shot]) -> Invalidated: ...

Reject different script_id or episode_id with ValueError. Compare beat contents by
beat_id: additions, removals and changed Beat values affect that beat. A changed,
added or removed beat_to_shot entry also affects that beat and both its old/new Shot
IDs. A changed beat affects old/new mapped Shot IDs even if its mapping is unchanged.
Approved Shots in that Script's episode are selected if they depend on an affected
beat ID, have an affected beat_id, or are an affected mapped Shot ID. Select each
Shot once, preserving input order. Non-approved and other-episode Shots are excluded.
An unchanged script or version/supersedes/approval-pointer-only edit invalidates
nothing. Logline-only changes and beat-order-only changes retain existing behavior
(no invalidation); broader narrative scope policy is outside this repair. Return a
useful reason and preserve all input objects/artifacts. No filesystem or GPU access.

service/dependencies.py loads candidate Shots, persists the upstream Script revision
and applies the returned domain decision through its existing cascade. It must not
compute beat dictionaries or define script-change scope locally. Existing public
signatures remain. A no-op revision is still persisted without staling Shots.

## Verification

Test author owns tests/domain/test_service_rules.py and
tests/service/test_boundary_integration.py only, and may read existing tests/store
interfaces for fixtures but MUST NOT read the concurrent implementation changes.
Use real ProjectStore and persisted Review records in integration tests, rather
than patched Review lookups. Cover approval success for Shot, Episode cut and
StyleBible, wrong target/version, rejected/missing Review, no unintended mutation,
and expected-version conflict. Cover Script beat edits/add/remove, mapping-only
remap (old and new Shot), beat_id/dependency/mapping linkage, unrelated and
non-approved Shots, no-op metadata, pure inputs and identity rejection. Check that
Script history, stale Shot artifact references and gate-C revocation survive real
store/reopen integration. Meaningful tests verify behavior, not implementation shape.

Implementer owns domain/state.py, domain/invalidation.py, service/approval.py and
service/dependencies.py only. It must not read new tests, edit tests or alter store
semantics. No extra packages/dependencies, GPU work or adapters in this repair.

## Explicitly outside this bounded repair

Read-only review exposed broader consistency concerns requiring their own contract:
per-content-version lifecycle CAS can lose concurrent updates; multi-record cascade
failure can retain gate-C approval after earlier Shots became stale; pre-revision
Shot snapshots can miss a concurrent approval. Do not claim this repair resolves
those, or that multi-record JSON mutations are atomic. Record adjudicated evidence
and follow-up ordering in HANDOFF before adapters are promoted.
