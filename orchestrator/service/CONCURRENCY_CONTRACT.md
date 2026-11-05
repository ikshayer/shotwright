# Service concurrency and recovery contract

This is the adjudication authority for SVC-01..03, subordinate to PRD 10.0,
10.2, AT-09, AT-12 and FR-25. The single orchestrator process remains the only
writer. Cross-process locking and physical power-loss guarantees are outside this
pass; deterministic write failures and reopen/recovery are in scope.

## Ownership and invariants

Domain owns approval, impact selection and dependency-freshness rules. Service
loads inputs, invokes domain rules, serializes commands and drives recovery. Store
provides CAS, append-only JSON persistence and opaque cascade-plan storage. No SQL
state, artifact deletion, relaxed transition edges, or content-version rebinding.

Content version (`entity.version`, `supersedes`) advances only on revision; a
Review still approves exactly (target ID, content version). The independent positive
integer mutation token advances on EVERY successful write (including lifecycle and
no-op writes). `expected_version` always means this token. There is NO compatibility
fallback accepting content version as a token. Creation implies expected 0. Legacy
versioned JSON without metadata starts with token equal to its content version;
unversioned legacy JSON starts at 1. Malformed metadata fails explicitly.

Versioned content files and lifecycle snapshots must be append-only. A permitted
lifecycle update appends an immutable snapshot for that SAME content version; it
must not overwrite the base content record. Each snapshot carries its mutation
token and predecessor token. Loading a historical content version returns its last
lifecycle snapshot; loading head returns the latest content version and its latest
lifecycle snapshot. Artifacts and prior snapshots remain intact. Unversioned records
retain their existing atomic replacement and advancing token semantics.

## Exact public interfaces

Preserve existing service function signatures and return shapes. All their
expected_version arguments now consume mutation tokens, not content versions.

ProjectStore.load_with_version(cls, record_id=None, *, episode_id=None,
                               version=None) -> tuple[E, int]
ProjectStore.update(entity: VersionedEntity, *, expected_version: int) -> None
ProjectStore.revise(entity: VersionedEntity, *, expected_version: int) -> None
ProjectStore.create(entity: VersionedEntity) -> None
ProjectStore.save(entity: Entity, *, expected_version: int) -> int

These methods use a shared resolved-project RLock across instances. Reads that
assemble a record/token snapshot also hold it. CAS compares the head mutation token,
then independently validates content-version/supersedes. VersionConflict retains
expected, actual and current for eventual API 409 mapping. Failed writes never
consume a token. Historical snapshots cannot overwrite the head.

ProjectStore.command() -> ContextManager[None]

Acquires the SAME shared project lock over the entire service command, from initial
reads through final commit. A nested SERVICE command on the same thread/root raises
CommandInProgress (a request error, not JobError); do not deadlock or silently allow
reentrant approval. Store reads/writes inside a command remain reentrant. Other
threads wait, then reread/CAS. Expose CommandInProgress from store/json_store.py.
This boundary covers every approval/revision command and recovery.

ProjectStore.record_cascade(plan: dict[str, Any]) -> None
ProjectStore.cascades(*, pending_only: bool = False) -> tuple[dict[str, Any], ...]
ProjectStore.complete_cascade(cascade_id: str) -> None

The store persists opaque immutable plans and append-only completion markers using
the existing atomic contained JSON writer. Plans have `cascade_id` (UUID hex),
`entity_type` (Script/Location/CameraProfile/Shot), `before` and `after` (domain JSON),
`expected_version` (upstream token), `episode_ids` (sorted unique IDs), and `affected`
(list of {episode_id, shot_id, version} for ALL affected existing Shot heads,
including review/draft, not just approved). Store validates journal structure on
read sufficiently to fail closed on malformed/corrupt plans; service validates
entity payloads and unknown types rather than skipping them. Returned history
includes completed plans because it is also the dependency-freshness evidence.
A completion marker contains cascade_id; a malformed marker is an error.

service.dependencies.resume_invalidations(store: ProjectStore) -> tuple[str, ...]

Runs under command serialization. Rolls ALL pending plans forward, returns their
cascade IDs in deterministic order, and is safe repeatedly and after reopening or
copying the JSON project. It is explicit, not a silent side effect of store reads.
If failure persists, raise with cause and leave the pending plan durable.

Extend domain/invalidation.py with:
revision_impact(before: Script | Location | CameraProfile | Shot,
                after: Script | Location | CameraProfile | Shot,
                shots: Iterable[Shot]) -> tuple[Shot, ...]

Pure, stable, deduplicated by (episode_id, shot_id); uses the existing Script beat/
mapping and per-field CameraProfile rules, but selects affected Shots of ALL states.
Location uses dependencies OR location_id linkage; Shot revision selects that Shot.
Post-only/no-op profile changes and existing Script no-op scope affect nothing.
Validate same entity identity/type; do not mutate inputs or change existing domain
approved-only selectors' behavior. Services must not duplicate impact policy.

Extend domain/state.py with:
check_dependency_freshness(shot: Shot, *,
    invalidated: Iterable[tuple[str, str, int]]) -> None
check_cut_approval(*, pending: bool, shots: Iterable[Shot]) -> None

Both pure; raise ReviewApprovalError. Invalidated tuples are (episode_id, shot_id,
content_version). A matching Shot version cannot be approved: rebuild a successor
and obtain its Review. Cut approval is blocked while pending or ANY current Shot in
the episode is stale/draft; other pre-existing cut policy is unchanged. Dependency
fences apply to completed AND pending invalidating plans. Shot successors are
fresh versions; they do not inherit approval or artifacts by overwrite.

## Revision protocol and failure/recovery semantics

Under command lock: reject other pending plans (JobError INVALID_MANIFEST with
resume_invalidations remediation), validate CAS and revision shape before any plan
or gate mutation, compute impact using domain, persist plan BEFORE any entity write.
Plans with no affected Shots may still be journaled. For revise_shot, the affected
episode gate must be revoked even if no approved Shot existed.

Then revoke ALL affected episode gates BEFORE upstream/Shot mutation; if any gate
write fails, stop without changing upstream or Shot state. A previously revoked gate
stays revoked. Commit upstream successor; then stale each affected currently
approved Shot at the planned content version using fresh mutation tokens. Already
stale Shots and an already-committed EXACT successor are idempotent successes. Never
revise the upstream twice. Review/draft affected Shots keep their states and are
fenced against later approval by the durable plan. Finally append completion marker.
Do not delete any plan, historical entity snapshot, or artifact.

An error at any phase leaves a durable pending plan and conservative gate state;
no service mutation except resume_invalidations proceeds until recovery finishes.
This includes cut/Shot/StyleBible approval and new revisions. Thus neither partial
cascades nor interrupted completion can be re-approved. Recovery revokes gates again
if necessary, identifies exact committed upstream payload, skips applied stale
writes and completes remaining work. Unexpected divergent upstream/Shot content
must fail explicitly, retaining the plan; never silently overwrite or abandon it.
Failures before plan persistence leave no entity changes. Validate all affected
Episode records exist before recording the plan. Error messages include plan ID,
progress/remediation, and cause; VersionConflict remains a conflict.

## Approval commit and the SVC-03 window

Approvals acquire the command boundary BEFORE any record, Review or dependency
snapshot. They refuse pending plans, load the target with token, validate Review
against CONTENT version, run domain freshness/cut guards using current cascade
history/current episode Shots, then reread target/token and dependency evidence
IMMEDIATELY before persistence. CAS uses the original expected token. If evidence
changed, fail closed; do not approve from a stale validation snapshot. The shared
lock excludes other-thread store mutations; same-thread nested service requests
are rejected. This closes the interval between validation and commit, rather than
merely moving the initial snapshot. Revision-before-approval fences old review
content; approval-before-revision is selected and made stale. Two stores rooted at
the same project participate in the same command boundary.

## Required negative cases and independent verification

Test actual ProjectStore JSON with persisted Reviews, no mocked Review success.
1. Two same-token lifecycle snapshots: notes write wins, stale approval/update
conflicts and cannot erase notes. Token advances for every legal lifecycle step,
no-op, content revision and unversioned save; Review target stays content version.
Reopen preserves token. Old content snapshots cannot write the new head. Retain
immutable lifecycle/base snapshots; rejected/failed writes do not advance token.
2. Inject failure at plan creation, each gate revocation, upstream append, first/
second stale write, completion marker. Reopen; never stale/draft under approved gate;
pending plans block all service approvals/revisions. Retry recovery repeatedly,
including a second failure; finish exactly once without extra upstream versions.
Retain artifacts/history. Cover revise_shot draft gate ordering, multiple episodes,
post-only profiles, Script mapping change, malformed journals and divergent recovery.
3. Deterministic interleavings: approval before revision becomes stale; revision
before approval refuses old review content; nested approval at upstream commit is
rejected; nested revision during approval validation is rejected; two same-root
stores share serialization. Revalidate dependency evidence at approval commit using
an injected history change; failure never writes approval. No GPU/subprocess/sleeps
in tests (thread events/barriers permitted with bounded timeout).

Existing tests hard-code content versions as CAS tokens. Independent test author
must migrate those call sites to load_with_version; this is a required contract
change, NOT weakened assertions. Keep substantive negative/immutability assertions.
The obsolete assertion that lifecycle updates leave only v0001.json must instead
assert retained base plus append-only lifecycle history and no temp files.

## File ownership and reports

Implementer (gpt-6-luna): production files under orchestrator/domain, store and
service only; may add at real responsibility boundaries. Never read/edit tests or
probe revisions. Test author (separate gpt-6.1-sol): tests/** plus supersede
_work/service-review/reproduce_consistency.py using advancing tokens and assertions
that all three defects are closed. Never read concurrent production edits; existing
interfaces and this contract are authoritative. Test author may read baseline source
via git show b386a28:path, and reports disagreements without fixing production.
Reviewer (gpt-6.1-sol): read-only contract/PRD/result inspection, report not fix.
All agents report what they could not verify. Parent reruns full pytest, ruff and
probes independently and adjudicates; work remains uncommitted.

## Parent adjudication addendum (after first independent run)

The first full run produced 610 passes and four honest failures; assertions are not
relaxed. Clarifications and required repair interfaces:

- Domain state owns revision shape, approval reset and draft-successor rules through
  `class RevisionError(ValueError)` and
  `check_revision(before: VersionedEntity, after: VersionedEntity) -> None`.
  It validates matching concrete type/identity and successor/supersedes rules.
- `ProjectStore.validate_revision(entity: VersionedEntity, *, expected_version: int)
  -> tuple[VersionedEntity, int]` performs locked CAS, Pydantic validation, the pure
  domain guard and declared-path containment without writing. Both store.revise and
  service preflight use it. Invalid input leaves no journal or gate writes.
- Journal `entity_type` is a nonempty string structurally; the service rejects
  unknown domain types at recovery (store does not own the domain type whitelist).
- Plan creation errors are mapped with cause before any entity changes. A durable
  plan ID and resume remediation are included if persistence may have succeeded.
- Changed dependency evidence at approval commit runs the domain freshness guard
  again and maps rejection to JobError INVALID_MANIFEST. A target token mismatch
  remains VersionConflict. Do not emit an expected=actual VersionConflict for a
  dependency change: that is not a target-token mismatch.
- Script all-state impact must reuse the existing Script selector, including
  unchanged mappings for changed beats, instead of maintaining parallel beat logic.
- Lifecycle predecessor tokens must form an advancing chain and snapshots must
  describe the same entity/content version as their base. Completion markers must
  match their filename's cascade ID; malformed JSON is INVALID_MANIFEST with cause.

No requirement is weakened by this addendum. New regression cases should cover
these boundaries independently; implementers must not read the test author's code.

### Final review hardening clarification

Token continuity includes cross-content revisions: when predecessor history exists,
the new base token must be exactly previous head token + 1, predecessor must equal
that previous token, and payload version must match filename. Legacy isolated
content files remain readable using the documented legacy rule. A malformed new
history cannot regress the CAS token and revive an old snapshot.

Service must validate ALL journal domain payloads, including COMPLETED plans, before
using them as approval/dependency evidence. Unknown type, wrong identity, impossible
successor/version or malformed before/after payload fails closed; completion is not
permission to skip validation. Structural store validation checks types before set
membership, so list/dict entity_type produces INVALID_MANIFEST, never raw TypeError.

If useful expose ProjectStore.validate_entity(entity: E) -> E to round-trip Pydantic
validation and declared path containment without writing, and use it in revision
preflight and history validation. Domain check_revision remains the rule owner.

The test author's broadening of dependency-evidence rejection from JobError to
(JobError, VersionConflict) is rejected: the target token did not change, and the
adjudicated contract requires re-running domain freshness and INVALID_MANIFEST.
Restore the precise assertion; true target-token races still require VersionConflict.

### Legacy continuity clarification

The legacy rule applies to multi-version histories as well as isolated files.
Records lacking mutation/predecessor metadata use their content version as token;
an older legacy base may already contain its last lifecycle state (including
approval), because baseline storage updated it in place. These schema-1 histories
must reopen without rewriting history. For legacy adjacent versions the synthetic
predecessor is version-1, provided previous actual head token agrees; do not let a
metadata-free successor regress a newer persisted lifecycle token. New explicitly
tracked base records still require exact persisted predecessor continuity and birth
revision policy. Content identity/version/supersedes remain validated for both.

### Lifecycle payload integrity adjudication

A lifecycle snapshot describes the SAME content, not merely the same identifiers.
On read, validate each adjacent lifecycle snapshot using the existing lifecycle-only
change/transition guard: content/artifact changes or illegal transitions in imported
JSON are INVALID_MANIFEST with cause. Reuse the existing guard, do not invent a
parallel content-field whitelist. On update, revalidate model_copy payloads through
the existing no-write Pydantic/containment validator AFTER CAS and BEFORE append;
write the validated model. A string approval state with a missing approval pointer
must not create an unreadable persisted head. Rejected writes retain all bytes and
the original token. This strengthens validation without changing legal transitions.
